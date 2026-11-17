"""A fake Boat API behind the real SDK: it stands in for the SDK's HTTP
client (Boat.transport), so every call goes through boat-sdk's own request
building and response models, and answers as docs.boat.dev/api/v1 says.

Sandboxes go provisioning -> ready on the next look, a stop archiving ->
archived, a resume is a cold boot (a new machine: /run and /tmp empty, the
boot counted). Commands answer through respond(sandbox_id, command) ->
stdout or (stdout, exit code); detached ones finish on their second poll.
fail(method, path_regex, status, code) makes the next matching call fail."""
from __future__ import annotations

import base64
import datetime
import itertools
import json
import re
import urllib.parse
from types import SimpleNamespace

NOW = "2026-11-17T12:00:00Z"


class FakeBoat:
    def __init__(self):
        self.sandboxes: dict = {}
        self.saved: dict = {}
        self.files: dict = {}           # sandbox id -> {path: bytes}
        self.processes: dict = {}       # process id -> {"polls", "stdout", "exit"}
        self.hosted: dict = {}          # (sandbox id, port) -> {"token", "localhost", "public"}
        self.calls: list = []           # (method, path, query, body)
        self.requests: list = []        # the same with the request's headers
        self.ran: list = []             # (sandbox id, command, detached)
        self.respond = None
        self.failures: list = []
        self.boots: dict = {}           # sandbox id -> machines it booted on
        self.page_size = 200
        self.plan = "20"
        self.keys = [{"keyLastFour": "test", "expiresAt": None}]
        self._ids = itertools.count(1)
        self.signed: dict = {}          # signed URL -> (sandbox id, path)

    # The SDK's HTTP client

    def request(self, method, url, headers=None, body=None, post_params=None, _request_timeout=None):
        parts = urllib.parse.urlsplit(url)
        path = parts.path.split("/api/v1", 1)[-1]
        query = urllib.parse.parse_qs(parts.query)
        self.calls.append((method, path, query, body))
        self.requests.append((method, path, query, body, dict(headers or {})))
        for index, (m, pattern, status, code, extra) in enumerate(self.failures):
            if m == method and re.fullmatch(pattern, path):
                del self.failures[index]
                return self._response(status, {"ok": False, "type": "sandbox.error", "status": status, "code": code,
                                               "message": code, "error": {"code": code, "message": code,
                                                                          "status": status, **extra}})
        status, payload = self._route(method, path, query, body or {}, headers or {})
        return self._response(status, payload)

    @staticmethod
    def _response(status, payload):
        from boat_sdk import rest
        return rest.RESTResponse(SimpleNamespace(status=status, reason="OK" if status < 300 else "Error",
                                                 data=json.dumps(payload).encode(),
                                                 headers={"content-type": "application/json"}))

    def fail(self, method, path, status, code, **extra):
        self.failures.append((method, path, status, code, extra))

    # Boat's answers

    def _sandbox(self, box):
        return {key: box[key] for key in ("id", "name", "state", "labels", "subdomain", "type", "vcpu", "memoryGB",
                                          "createdAt", "archiveAfter", "stoppedAt", "ttlSeconds", "error")} | {
            "desktopAvailable": box["state"] in ("ready", "idle", "running"), "snapshotAvailable": True,
            "hydrated": box.get("hydrated")}

    def _missing(self, what="not_found"):
        return 404, {"ok": False, "type": "sandbox.error", "status": 404, "code": what, "message": what,
                     "error": {"code": what, "message": what, "status": 404}}

    def _route(self, method, path, query, body, headers):
        parts = path.strip("/").split("/")
        if parts == ["sandboxes"] and method == "POST":
            return self._create(body)
        if parts == ["sandboxes"] and method == "GET":
            return self._list(query)
        if parts[0] == "sandboxes" and len(parts) >= 2:
            box = self.sandboxes.get(parts[1])
            if box is None:
                return self._missing()
            return self._sandbox_call(method, box, parts[2:], body, headers, query)
        if parts == ["saved-snapshots"] and method == "POST":
            name = body["name"]
            source = self.sandboxes[body["sandboxId"]]
            self.saved[name] = {"name": name, "status": "saving", "sourceSandboxId": source["id"],
                                "createdAt": NOW, "type": source["type"],
                                "files": dict(self.files.get(source["id"], {}))}
            return 202, {"ok": True, "type": "snapshot.saved.saving", "status": "saving",
                         "snapshot": self._saved(self.saved[name])}
        if parts == ["saved-snapshots"] and method == "GET":
            return 200, {"ok": True, "type": "snapshot.saved.list", "snapshots": [self._saved(s) for s in self.saved.values()]}
        if parts[0] == "saved-snapshots" and len(parts) == 2:
            found = self.saved.get(parts[1])
            if found is None:
                return self._missing("snapshot_not_found")
            if method == "DELETE":
                del self.saved[parts[1]]
                return 200, {"ok": True, "type": "snapshot.saved.deleted", "name": parts[1], "status": "deleted"}
            shown = self._saved(found)
            found["status"] = "ready"   # saved by the next look
            return 200, {"ok": True, "type": "snapshot.saved.info", "snapshot": shown}
        if parts == ["limits"]:
            return 200, {"ok": True, "type": "limits", "canStart": True, "billingStatus": "active",
                         "activeSandboxes": sum(b["state"] in ("ready", "provisioning") for b in self.sandboxes.values()),
                         "maxActiveSandboxes": 100, "sandboxPlanKey": self.plan}
        if parts == ["api-keys"]:
            return 200, {"ok": True, "type": "api_keys", "apiKeys": self.keys}
        return 404, {"ok": False, "type": "sandbox.error", "code": "not_found", "message": path}

    @staticmethod
    def _saved(s):
        return {k: s[k] for k in ("name", "status", "sourceSandboxId", "createdAt", "type")}

    def _create(self, body):
        source = body.get("from")
        if source and source not in self.saved:
            return self._missing("snapshot_not_found")
        sandbox_id = sandbox_id_for(next(self._ids))
        kind = body.get("type") or (self.saved[source]["type"] if source else "default")
        size = {"small": (2, 4), "default": (4, 8), "large": (8, 16), "xlarge": (16, 32)}[kind]
        box = {"id": sandbox_id, "name": body.get("name") or sandbox_id, "state": "provisioning",
               "labels": body.get("labels") or {}, "subdomain": f"word-{sandbox_id[3:]}", "type": kind,
               "vcpu": size[0], "memoryGB": size[1], "createdAt": now(), "archiveAfter": NOW, "stoppedAt": None,
               "ttlSeconds": body.get("ttlSeconds"), "error": None, "from": source, "env": body.get("env"),
               "noEnv": body.get("noEnv")}
        self.sandboxes[sandbox_id] = box
        self.files[sandbox_id] = {p: v for p, v in (self.saved[source]["files"] if source else {}).items()
                                  if not p.startswith(("/run/", "/tmp/"))}
        self.boots[sandbox_id] = 1
        return 202, {"ok": True, "type": "sandbox.created", "status": "provisioning",
                     "ttlSeconds": body.get("ttlSeconds"), "sandbox": self._sandbox(box)}

    def _list(self, query):
        wanted = set(",".join(query.get("state", [])).split(",")) - {""}
        labels = [label.partition("=") for label in query.get("label", [])]
        found = [b for b in self.sandboxes.values() if (not wanted or b["state"] in wanted)
                 and all(b["labels"].get(k) == v for k, _, v in labels)]
        start = int(query.get("cursor", ["0"])[0])
        page = found[start:start + self.page_size]
        more = start + self.page_size < len(found)
        return 200, {"ok": True, "type": "sandbox.list", "sandboxes": [self._sandbox(b) for b in page],
                     "pageInfo": {"nextCursor": str(start + self.page_size) if more else None, "hasMore": more,
                                  "limit": self.page_size}}

    def _settle(self, box):
        """The next look: started; then (a sandbox from a snapshot) its files all in."""
        if box["state"] == "ready" and box.get("hydrated") is False:
            box["hydrated"] = True
        elif box["state"] == "provisioning":
            box["state"] = "ready"
        elif box["state"] == "archiving":
            box["state"] = "archived"

    def _sandbox_call(self, method, box, rest, body, headers, query):
        sandbox_id = box["id"]
        up = box["state"] in ("ready", "idle", "running")
        if not rest:
            if method == "GET":
                shown = self._sandbox(box)
                self._settle(box)
                return 200, {"ok": True, "type": "sandbox.info", "sandbox": shown}
            if method == "PATCH":
                box["ttlSeconds"] = body.get("ttlSeconds", box["ttlSeconds"])
                return 200, {"ok": True, "type": "sandbox.info", "sandbox": self._sandbox(box)}
            if method == "DELETE":
                if headers.get("X-Ascii-Confirm-Delete") != sandbox_id:
                    return 409, {"ok": False, "type": "sandbox.error", "code": "confirmation_required"}
                del self.sandboxes[sandbox_id]
                return 202, {"ok": True, "type": "deletion.operation", "operation": {
                    "id": "bdop_" + "0" * 32, "kind": "sandbox", "targetId": sandbox_id, "reason": "explicit", "status": "pending",
                    "attemptCount": 0, "requestedAt": NOW}}
        what = rest[0]
        if what == "stop":
            if box["state"] in ("archiving", "archived"):
                return 409, {"ok": False, "type": "sandbox.error", "code": "sandbox_stopping", "message": "stopping"}
            box["state"] = "archiving"
            for key in [k for k in self.hosted if k[0] == sandbox_id]:
                self.hosted[key]["machine_gone"] = True
            return 202, {"ok": True, "type": "sandbox.stopping", "id": sandbox_id, "status": "archiving"}
        if what == "resume":
            if box["state"] != "archived" and box["state"] != "error":
                return 409, {"ok": False, "type": "sandbox.error", "code": "sandbox_running_stop_first"}
            box["state"] = "provisioning"
            box["ttlSeconds"] = body.get("ttlSeconds", box["ttlSeconds"])
            self.boots[sandbox_id] += 1
            self.files[sandbox_id] = {p: v for p, v in self.files[sandbox_id].items()
                                      if not p.startswith(("/run/", "/tmp/"))}   # a new machine
            return 202, {"ok": True, "type": "sandbox.resuming", "id": sandbox_id, "status": "provisioning"}
        if not up:
            return 409, {"ok": False, "type": "sandbox.error", "status": 409, "code": "machine_not_running",
                         "message": "machine_not_running", "error": {"code": "machine_not_running"}}
        if what == "commands" and len(rest) == 1:
            return self._command(sandbox_id, body)
        if what == "commands" and len(rest) == 2:
            process = self.processes[int(rest[1])]
            process["polls"] += 1
            running = process["polls"] < 2
            return 200, {"ok": True, "type": "command.status", "success": True, "processId": int(rest[1]),
                         "status": "running" if running else "exited", "running": running,
                         "exitCode": None if running else process["exit"],
                         "stdout": "" if running else process["stdout"], "stderr": "",
                         "stdoutNextOffset": 0 if running else len(process["stdout"]), "stderrNextOffset": 0}
        if what == "files" and len(rest) == 1:
            path = query.get("path", [""])[0] if method == "GET" else body["path"]
            if not (path.startswith(("/home/user/", "/tmp/")) or not path.startswith("/")):
                return 400, {"ok": False, "type": "sandbox.error", "code": "invalid_path"}
            if method == "PUT":
                self.files[sandbox_id][path] = base64.b64decode(body["content"])
                return 200, {"ok": True, "type": "file.written", "success": True, "path": path,
                             "encoding": "base64", "size": len(self.files[sandbox_id][path])}
            if path not in self.files[sandbox_id]:
                return self._missing()
            data = self.files[sandbox_id][path]
            if len(data) > 5 * 1024 * 1024:
                return 413, {"ok": False, "type": "sandbox.error", "code": "file_too_large",
                             "message": "file_too_large", "error": {"code": "file_too_large"}}
            return 200, {"ok": True, "type": "file.read", "success": True, "path": path, "encoding": "base64",
                         "size": len(data), "content": base64.b64encode(data).decode()}
        if what == "files" and rest[1] in ("upload-url", "download-url"):
            url = f"https://files.example.test/{sandbox_id}/{len(self.signed)}"
            self.signed[url] = (sandbox_id, body["path"])
            return 200, {"ok": True, "type": "file.upload_url" if rest[1] == "upload-url" else "file.download_url", "url": url, "method": "PUT" if rest[1] == "upload-url" else "GET",
                         "path": body["path"], "expiresAt": NOW}
        if what == "host":
            key = (sandbox_id, body["port"])
            kept = self.hosted.get(key)
            token = kept["token"] if kept and not body.get("public") else f"tok-{sandbox_id}-{body['port']}"
            self.hosted[key] = {"token": token, "localhost": body.get("localhost"), "public": body.get("public")}
            url = f"https://{box['subdomain']}-{body['port']}.on.boat.dev" + ("" if body.get("public") else f"?_token={token}")
            return 200, {"ok": True, "type": "host.port", "port": body["port"], "url": url}
        if what == "desktop":
            return 200, {"ok": True, "type": "desktop", "desktopUrl": f"https://desk.example.test/stream.html#token=d-{sandbox_id}"}
        return 404, {"ok": False, "type": "sandbox.error", "code": "not_found"}

    def _command(self, sandbox_id, body):
        command = body["command"]
        self.ran.append((sandbox_id, command, bool(body.get("detached"))))
        answer = self.respond(sandbox_id, command) if self.respond else None
        if answer is None:
            answer = self._interpret(sandbox_id, command)
        stdout, code = (answer, 0) if isinstance(answer, str) else answer
        if body.get("detached"):
            process_id = len(self.processes) + 1
            self.processes[process_id] = {"polls": 0, "stdout": stdout, "exit": code}
            return 200, {"ok": True, "type": "command.started", "success": True, "processId": process_id,
                         "pid": 1000 + process_id, "command": command, "startedAt": NOW}
        return 200, {"ok": True, "type": "command.finished", "success": code == 0, "exitCode": code, "stdout": stdout,
                     "stderr": "" if code == 0 else stdout, "timedOut": False}

    def _interpret(self, sandbox_id, command):
        """mkdir/mv of an upload, base64 -w0 of a file, printf of a plain word, test -f: enough for the backend's own commands."""
        files = self.files[sandbox_id]
        moved = re.search(r"mv -f (\S+) (\S+)", command)
        if moved and moved.group(1) in files:
            files[moved.group(2).strip("'")] = files.pop(moved.group(1))
            return ""
        shown = re.search(r"base64 -w0 (\S+)", command)
        if shown:
            path = shown.group(1).strip("'")
            return (base64.b64encode(files[path]).decode(), 0) if path in files else ("", 1)
        printed = re.search(r'printf %s "([^"$`\\]*)"', command)
        if printed:
            return printed.group(1)
        tested = re.search(r"test -f (\S+)", command)
        if tested:
            return "ok\n" if tested.group(1) in files else ""
        return ""

    # Signed URLs (Boat.http_open)

    def http_open(self, url, method, data):
        sandbox_id, path = self.signed[url]
        if method == "PUT":
            self.files[sandbox_id][path] = data
            return b""
        return self.files[sandbox_id][path]


ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def sandbox_id_for(number: int) -> str:
    """Boat's id shape: bx_ and 8 of its letters."""
    letters = ""
    for _ in range(8):
        number, digit = divmod(number, len(ALPHABET))
        letters = ALPHABET[digit] + letters
    return "bx_" + letters


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def configured(fake: FakeBoat | None = None, **secrets):
    """A Boat backend configured against the fake (its key ends in "test")."""
    from sbx_control.runtime.boat import Boat
    fake = fake or FakeBoat()
    backend = Boat()
    backend.configure({"BOAT_API_KEY": "boat_unit-test", **secrets})
    backend.transport = fake
    backend.http_open = fake.http_open
    return backend, fake


def utc(text=NOW):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
