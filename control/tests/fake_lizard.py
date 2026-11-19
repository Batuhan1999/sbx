"""A fake Lizard API: it stands in for the backend's HTTP requests
(Lizard.transport) and answers as lizard-sdk calls the real one
(github.com/lizard-build/lizard-sdk, packages/python/lizard: sandbox.py,
process.py, fs.py, errors.py).

Sandboxes are running once created; a pause keeps the files and a resume is
a fresh boot (counted in boots; what was published is gone). Commands answer
through respond(sandbox_id, script) -> stdout or (stdout, exit code); a
detached one (the backend's nohup launcher) finishes on its second poll.
fail(method, path_regex, status, body) makes the next matching call fail."""
from __future__ import annotations

import base64
import itertools
import json
import re
import urllib.parse

NOW = 1795000000000   # unix ms


class FakeLizard:
    def __init__(self):
        self.projects = [{"id": "proj_1", "slug": "main", "name": "Main"}]
        self.sandboxes: dict = {}
        self.snapshots: dict = {}
        self.files: dict = {}        # sandbox id -> {path: (bytes, user)}
        self.exposed: dict = {}      # (sandbox id, port) -> True
        self.detached: dict = {}     # folder -> {"script", "polls"}
        self.calls: list = []        # (method, path, query, body)
        self.headers: list = []
        self.ran: list = []          # (sandbox id, cmd, user, envs)
        self.boots: dict = {}
        self.respond = None
        self.failures: list = []
        self.tier = "pro"
        self.region_domain = "sandbox.us-east-1.onlizard.com"
        self.token = "tok-"
        self._ids = itertools.count(1)

    def fail(self, method, pattern, status, body=None, times=1):
        self.failures += [(method, pattern, status, body if body is not None else {"error": "failed"})] * times

    def snapshot(self, name, **fields):
        snapshot_id = f"snap_{next(self._ids)}"
        self.snapshots[snapshot_id] = {"id": snapshot_id, "name": name, "status": "ready", "projectId": "proj_1",
                                       "region": "us-east-1", "createdAt": NOW + len(self.snapshots),
                                       "cpus": 2, "memoryMb": 4096, **fields}
        return snapshot_id

    # The transport: (status, body bytes)

    def __call__(self, method, url, headers, data, timeout):
        parts = urllib.parse.urlsplit(url)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        body = json.loads(data) if data else None
        self.calls.append((method, parts.path, query, body))
        self.headers.append(dict(headers))
        for index, (m, pattern, status, answer) in enumerate(self.failures):
            if m == method and re.fullmatch(pattern, parts.path):
                del self.failures[index]
                return status, answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        if headers.get("X-API-Key") != "liz_unit-test":
            return 401, b'{"error":"Unauthorized"}'
        status, answer = self._route(method, parts.path, query, body or {})
        return status, answer if isinstance(answer, bytes) else json.dumps(answer).encode()

    def _route(self, method, path, query, body):
        if (method, path) == ("GET", "/api/projects"):
            return 200, self.projects
        if (method, path) == ("GET", "/api/sandboxes/limits"):
            return 200, {"tier": self.tier, "unit": "vcpu", "limits": {}, "running": len(self.sandboxes),
                         "runningVcpu": 2 * len(self.sandboxes)}
        if (method, path) == ("POST", "/api/sandboxes"):
            return self._create(body)
        found = re.fullmatch(r"/api/projects/([^/]+)/(sandboxes|snapshots)", path)
        if found and method == "GET":
            kept = self.sandboxes if found.group(2) == "sandboxes" else self.snapshots
            return 200, [dict(v) for v in kept.values() if v.get("projectId") == found.group(1)]
        found = re.fullmatch(r"/api/sandbox-snapshots/([^/]+)", path)
        if found:
            if found.group(1) not in self.snapshots:
                return 404, {"error": "Snapshot not found"}
            if method == "DELETE":
                del self.snapshots[found.group(1)]
                return 200, {"ok": True}
            return 200, self.snapshots[found.group(1)]
        found = re.fullmatch(r"/api/sandboxes/([^/]+)(/.*)?", path)
        if not found or found.group(1) not in self.sandboxes:
            return 404, {"error": "Sandbox not found"}
        return self._sandbox(method, found.group(1), found.group(2) or "", query, body)

    def _create(self, body):
        if not body.get("projectId"):
            return 400, {"error": "PROJECT_REQUIRED", "message": "A project is required"}
        source = self.snapshots.get(body.get("snapshotId")) if body.get("snapshotId") else None
        if body.get("snapshotId") and source is None:
            return 404, {"error": "Snapshot not found"}
        sizes = {"mini": (1, 2048), "small": (2, 4096), "medium": (4, 8192), "large": (8, 16384), "big": (16, 32768)}
        cpus, memory = (source["cpus"], source["memoryMb"]) if source else sizes[body.get("size") or "medium"]
        sandbox_id = f"sb{next(self._ids)}"
        self.sandboxes[sandbox_id] = {
            "sandboxId": sandbox_id, "status": "running", "projectId": body["projectId"],
            "template": body.get("template") or "", "metadata": dict(body.get("metadata") or {}),
            "startedAt": "2026-11-19T12:00:00.000Z", "region": body.get("region") or "us-east-1",
            "endAt": "2026-11-19T13:00:00.000Z" if body.get("timeoutMs") else None, "cpus": cpus,
            "memoryMb": memory, "timeoutMs": body.get("timeoutMs")}
        self.files[sandbox_id] = dict((source or {}).get("files") or {})
        self.boots[sandbox_id] = 1
        return 201, {"sandboxId": sandbox_id}

    def _sandbox(self, method, sandbox_id, rest, query, body):
        sandbox = self.sandboxes[sandbox_id]
        if rest == "" and method == "GET":
            return 200, dict(sandbox)
        if rest == "" and method == "DELETE":
            del self.sandboxes[sandbox_id]
            return 200, {"ok": True}
        if rest == "/pause":
            sandbox["status"] = "paused"
            for key in [k for k in self.exposed if k[0] == sandbox_id]:
                del self.exposed[key]
            return 200, {"ok": True}
        if rest == "/resume":
            sandbox["status"] = "running"
            self.boots[sandbox_id] += 1
            return 200, {"ok": True}
        if rest == "/timeout":
            sandbox["timeoutMs"] = body["timeoutMs"]
            return 200, {"ok": True}
        if sandbox["status"] != "running":
            return 409, {"error": "Sandbox is not running"}
        if rest == "/exec":
            return 200, self._exec(sandbox_id, body)
        if rest == "/files" and method == "POST":
            raw = base64.b64decode(body["content"]) if body.get("encoding") == "base64" else body["content"].encode()
            self.files[sandbox_id][body["path"]] = (raw, body.get("user") or "root")
            return 200, {"ok": True}
        if rest == "/files" and method == "GET":
            if query["path"] not in self.files[sandbox_id]:
                return 404, {"error": "No such file"}
            return 200, self.files[sandbox_id][query["path"]][0]
        found = re.fullmatch(r"/expose/(\d+)", rest)
        if found:
            self.exposed[(sandbox_id, int(found.group(1)))] = True
            host = f"{sandbox_id}-{found.group(1)}.{self.region_domain}"
            return 200, {"hostname": host, "url": f"https://{host}/?lizard_token={self.token}{sandbox_id}",
                         "port": int(found.group(1)), "accessToken": self.token and self.token + sandbox_id}
        if rest == "/snapshot":
            snapshot_id = self.snapshot(body["name"], sourceSandboxId=sandbox_id, cpus=sandbox["cpus"],
                                        memoryMb=sandbox["memoryMb"], files=dict(self.files[sandbox_id]))
            return 200, {k: v for k, v in self.snapshots[snapshot_id].items() if k != "files"}
        return 404, {"error": "Not found"}

    def _answer(self, sandbox_id, script):
        found = self.respond(sandbox_id, script) if self.respond else ""
        return found if isinstance(found, tuple) else (found or "", 0)

    def _exec(self, sandbox_id, body):
        cmd = body["cmd"]
        self.ran.append((sandbox_id, cmd, body.get("user"), body.get("envs")))
        if isinstance(cmd, list) and cmd[:2] == ["bash", "-c"] and "nohup setsid" in cmd[2] and len(cmd) == 6:
            self.detached[cmd[4]] = {"script": cmd[5], "polls": 0}   # the backend's launcher: folder, script
            return {"stdout": "started\n", "stderr": "", "exitCode": 0}
        if isinstance(cmd, list) and cmd[:2] == ["bash", "-c"] and len(cmd) == 7 and cmd[4] in self.detached:
            process = self.detached[cmd[4]]   # the backend's poll: folder, the offsets
            process["polls"] += 1
            out, code = self._answer(sandbox_id, process["script"]) if process["polls"] == 1 else ("", 0)
            process.setdefault("code", code)
            done = str(process["code"]) if process["polls"] >= 2 else ""
            return {"stdout": f"here\n{base64.b64encode(out.encode()).decode()}\n\n{done}\n", "stderr": "",
                    "exitCode": 0}
        if isinstance(cmd, list) and cmd[:2] == ["bash", "-c"] and len(cmd) == 7:
            return {"stdout": "gone\n", "stderr": "", "exitCode": 0}
        script = cmd[2] if isinstance(cmd, list) and cmd[:2] == ["bash", "-c"] else cmd
        if isinstance(cmd, list) and cmd[:2] == ["bash", "-c"] and "cat \"$@\"" in cmd[2]:   # a file's parts joined
            parts = b"".join(self.files[sandbox_id].pop(name)[0] for name in cmd[5:])
            self.files[sandbox_id][cmd[4]] = (parts, body.get("user") or "root")
        out, code = self._answer(sandbox_id, script if isinstance(script, str) else " ".join(script))
        return {"stdout": out, "stderr": "boom" if code else "", "exitCode": code}


def configured(fake: FakeLizard | None = None, **secrets):
    """A Lizard backend configured against the fake."""
    from sbx_control.runtime.lizard import Lizard
    fake = fake or FakeLizard()
    backend = Lizard()
    backend.configure({"LIZARD_API_KEY": "liz_unit-test", **secrets})
    backend.transport = fake
    return backend, fake
