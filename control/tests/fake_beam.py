"""A fake of Beam's Gateway HTTP API (app.beam.cloud/api/v1/gateway), as
runtime/beam speaks it: it stands in for the backend's HTTP client
(Beam.transport), so the tests run the backend's real requests. Answers are
shaped as Beam's protos say and as the live service answered a bad token
(401 {"code": 16, ...}); sandboxes are dicts of files, and a command is
interpreted just far enough for what the backend itself runs."""
import base64
import json
import re
import shlex
import urllib.parse

TOKEN = "beam-unit-test"
BASE = "https://app.beam.cloud/api/v1/gateway"
TEMPLATE_PREFIX = re.compile(r"^\[ -r /etc/sbx/template\.sh \] && \. /etc/sbx/template\.sh; (?:cd \S+ \|\| exit 1; "
                             r"|cd \"\$\{SBX_TEMPLATE_WORKDIR:-\$HOME\}\" 2>/dev/null; )", re.S)


class FakeBeam:
    def __init__(self):
        self.calls = []        # (method, path, query, body) of every API request, in order
        self.images = {"img-ubuntu": {}}                 # image id -> files
        self.registry = {"docker.io/library/ubuntu:24.04": "img-ubuntu"}
        self.boxes = {}        # container id -> {"stub", "files", "status", "ttl", "listening", "foreign"}
        self.stubs = {}        # stub id -> the request that made it
        self.processes = {}    # (container id, pid) -> {"code", "out", "err", "polls"}
        self.ran = []          # (container id, user, cwd, command, env) of every command
        self.respond = None    # (container id, command) -> None, stdout, or (stdout, exit code)
        self.failures = {}     # (method, path) -> [(status, body), ...] answered first
        self.hang = None       # a command text that never ends
        self.preview_domain = "app.beam.cloud"
        self.puts = []         # presigned uploads: (url, data)
        self._n = 0

    def fail(self, method, path, status, body, times=1):
        self.failures.setdefault((method, path), []).extend([(status, body)] * times)

    def __call__(self, method, url, headers, data, timeout):
        if not url.startswith(BASE):
            self.puts.append((url, data))
            return 200, b""
        parts = urllib.parse.urlsplit(url[len(BASE):])
        path, query = urllib.parse.unquote(parts.path), dict(urllib.parse.parse_qsl(parts.query))
        body = json.loads(data) if data else None
        self.calls.append((method, path, query, body))
        if self.failures.get((method, path)):
            status, answer = self.failures[(method, path)].pop(0)
            return status, json.dumps(answer).encode()
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            if path == "/auth/authorize":
                return 200, json.dumps({"ok": False, "workspaceId": "", "newToken": "", "errorMsg": "Invalid token"}).encode()
            return 401, json.dumps({"code": 16, "message": "invalid or missing token", "details": []}).encode()
        answer = self._route(method, path, query, body or {})
        if isinstance(answer, bytes):
            return 200, answer
        return 200, json.dumps(answer).encode()

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}-{self._n}"

    def _route(self, method, path, query, body):
        if path == "/auth/authorize":
            return {"ok": True, "workspaceId": "ws-1", "newToken": "", "errorMsg": ""}
        if path.startswith("/objects/"):
            return {"ok": True, "exists": False, "objectId": ""}
        if path == "/objects":
            return {"ok": True, "objectId": "obj-1", "presignedUrl": "https://storage.example/put", "putHeaders": {}}
        if path == "/images/verify-build":
            image = body.get("imageId") or self.registry.get(body.get("existingImageUri"))
            return {"imageId": image or "img-new", "valid": True, "exists": image in self.images}
        if path == "/images/build":
            self.images["img-new"] = {}
            lines = [{"result": {"msg": "pulling\n", "done": False}},
                     {"result": {"imageId": "img-new", "msg": "", "done": True, "success": True}}]
            return "\n".join(json.dumps(line) for line in lines).encode()
        if path == "/stubs":
            if body.get("imageId") not in self.images:
                return {"ok": False, "stubId": "", "errMsg": "image not found"}
            stub = self._id("stub")
            self.stubs[stub] = body
            return {"ok": True, "stubId": stub, "errMsg": ""}
        if path == "/pods":
            stub = self.stubs[body["stubId"]]
            box = self._id("sandbox")
            self.boxes[box] = {"stub": body["stubId"], "files": dict(self.images[stub["imageId"]]), "status": "RUNNING",
                               "ttl": stub["keepWarmSeconds"], "listening": set(), "started": False}
            return {"ok": True, "containerId": box, "errorMsg": "", "stubId": body["stubId"]}
        if path == "/containers":
            return {"ok": True, "errorMsg": "", "containers": [
                {"containerId": box, "stubId": found["stub"], "status": found["status"],
                 "startedAt": "2026-11-19T12:00:00Z"} for box, found in self.boxes.items()]}
        stop = re.fullmatch(r"/containers/([^/]+)/stop", path)
        if stop:
            return {"ok": self.boxes.pop(stop.group(1), None) is not None, "errorMsg": "container not found"}
        pod = re.fullmatch(r"/pods/([^/]+)(/.*)", path)
        if pod:
            box = self.boxes.get(pod.group(1))
            if box is None or box.get("foreign"):
                return {"ok": False, "errorMsg": "Failed to connect to sandbox" if box else "sandbox not found"}
            return self._sandbox(pod.group(1), box, pod.group(2), query, body)
        return {"code": 5, "message": "Not Found"}

    def _sandbox(self, box_id, box, rest, query, body):
        if rest == "/connect":
            return {"ok": True, "errorMsg": "", "stubId": box["stub"]}
        if rest == "/ttl":
            box["ttl"] = body["ttl"]
            return {"ok": True, "errorMsg": ""}
        if rest == "/ports/expose":
            return {"ok": True, "errorMsg": "", "url": f"https://{box['stub']}-{body['port']}.{self.preview_domain}"}
        if rest == "/create-image-from-filesystem":
            image = self._id("img")
            self.images[image] = dict(box["files"])
            return {"ok": True, "errorMsg": "", "imageId": image}
        if rest == "/files/upload":
            data = base64.b64decode(body["data"])
            before = box["files"].get(body["containerPath"], b"") if body.get("offset") else b""
            box["files"][body["containerPath"]] = before[:body.get("offset", 0)] + data
            return {"ok": True, "errorMsg": ""}
        if rest == "/exec":
            pid = self._n = self._n + 1
            out, code = self._run(box_id, box, body)
            self.processes[(box_id, pid)] = {"code": code, "out": out, "err": "" if code == 0 else "failed\n",
                                             "polls": 1}
            return {"ok": True, "errorMsg": "", "pid": pid, "done": False, "exitCode": -1, "stdout": "", "stderr": ""}
        process = self.processes.get((box_id, int(query.get("pid") or body.get("pid") or 0)))
        if process is None:
            return {"ok": False, "errorMsg": "process not found"}
        if rest == "/status":
            process["polls"] -= 1
            running = process["polls"] >= 0 or process["code"] is None
            return {"ok": True, "errorMsg": "", "status": "running" if running else "exited",
                    "exitCode": -1 if running else process["code"]}
        if rest in ("/stdout", "/stderr"):
            key = "out" if rest == "/stdout" else "err"
            text, process[key] = process[key], ""   # a delta: cleared once read
            return {"ok": True, "errorMsg": "", rest[1:]: text}
        if rest == "/kill":
            process["code"] = 137
            return {"ok": True, "errorMsg": ""}
        return {"ok": False, "errorMsg": f"unknown route {rest}"}

    def _run(self, box_id, box, body):
        words = shlex.split(body["command"])
        user = "root"
        if len(words) == 5:   # bash -c <switch> sbx <script>
            user = "default" if "id -u user" in words[2] else words[2].split()[-1]
        script = words[-1]
        cwd = re.match(r".*?; cd (\S+) \|\| exit 1; ", script)
        command = TEMPLATE_PREFIX.sub("", script)
        self.ran.append((box_id, user, cwd.group(1) if cwd else None, command, body.get("env") or {}))
        files = box["files"]
        if self.hang is not None and command == self.hang:
            return "", None
        if self.respond is not None:
            answer = self.respond(box_id, command)
            if answer is not None:
                return answer if isinstance(answer, tuple) else (answer, 0)
        if match := re.fullmatch(r"base64 -w0 -- (\S+)", command):
            path = shlex.split(match.group(1))[0]
            return (base64.b64encode(files[path]).decode(), 0) if path in files else ("", 1)
        if match := re.match(r"mkdir -p \S+ && mv -f (\S+) (\S+) && chown ", command):
            files[shlex.split(match.group(2))[0]] = files.pop(match.group(1))
            return "", 0
        if match := re.fullmatch(r"mkdir -p \S+ && echo (\S+) \| base64 -d > (\S+) && chmod 644 \S+", command):
            files[match.group(2)] = base64.b64decode(match.group(1))
            return "", 0
        if command.startswith("cat /etc/sbx/template.sh"):
            return files.get("/etc/sbx/template.sh", b"").decode(), 0
        if command.startswith("exec bash /etc/sbx/start.sh"):
            box["started"] = True
            return "", None   # it keeps running
        if match := re.match(r"ss -Hltn 'sport = :(\d+)'", command):
            return (f"LISTEN 0 511 0.0.0.0:{match.group(1)} 0.0.0.0:*\n" if int(match.group(1)) in box["listening"]
                    else ""), 0
        if match := re.fullmatch(r"echo (.*)", command):
            return match.group(1) + "\n", 0
        if match := re.fullmatch(r"exit (\d+)", command):
            return "", int(match.group(1))
        return "", 0


def configured(fake: FakeBeam | None = None, index=None, **secrets):
    """A Beam backend configured against the fake."""
    from sbx_control.runtime.beam import Beam
    fake = fake or FakeBeam()
    backend = Beam()
    backend.configure({"BEAM_API_KEY": TOKEN, **secrets})
    backend.transport = fake
    backend.index_path = index
    return backend, fake
