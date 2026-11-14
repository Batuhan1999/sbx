"""An Infisical on a loopback port: the parts of its HTTP API sbx uses
(universal-auth login, folders, v4 secrets, webhooks, the admin bootstrap),
held in memory. Tests look at `secrets` (path -> name -> value) and `webhooks`;
`down` makes it refuse every request, `lose_writes` accept writes it does not keep."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit


class FakeInfisical:
    def __init__(self):
        self.secrets: dict = {}
        self.webhooks: list = []
        self.down = False
        self.lose_writes = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status, body=None):
                data = json.dumps(body if body is not None else {}).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                parsed = urlsplit(self.path)
                if owner.down:
                    return self._answer(503)
                status, answer = owner.route(self.command, parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()},
                                             body, self.headers.get("authorization", ""))
                self._answer(status, answer)

            do_GET = do_POST = do_PATCH = do_DELETE = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def route(self, method, path, query, body, auth):
        if path == "/api/status":
            return 200, {"message": "ok"}
        if path == "/api/v1/auth/universal-auth/login":
            if body.get("clientId") and body.get("clientSecret"):
                return 200, {"accessToken": "fixture-token", "expiresIn": 7200}
            return 401, {}
        if not auth.startswith("Bearer "):
            return 401, {}
        if path == "/api/v1/admin/bootstrap":
            return 200, {"identity": {"credentials": {"token": "root"}}, "organization": {"id": "org"}}
        if path == "/api/v1/projects":
            return 200, {"project": {"id": "fixture-project"}}
        if path == "/api/v1/identities":
            return 200, {"identity": {"id": "identity"}}
        if path.startswith("/api/v1/auth/universal-auth/identities/"):
            if path.endswith("/client-secrets"):
                return 200, {"clientSecret": "fixture-client-secret"}
            return 200, {"identityUniversalAuth": {"clientId": "fixture-client-id"}}
        if "/identity-memberships/" in path:
            return 200, {}
        if path == "/api/v1/folders":
            return 200, {"folder": {"id": "folder"}}
        if path == "/api/v1/webhooks":
            if method == "POST":
                self.webhooks.append({"url": body["webhookUrl"], "secretPath": body["secretPath"]})
                return 200, {}
            return 200, {"webhooks": self.webhooks}
        if path == "/api/v4/secrets" and method == "GET":
            folder = self.secrets.get(query.get("secretPath"), {})
            return 200, {"secrets": [{"secretKey": k, "secretValue": v} for k, v in folder.items()]}
        if path.startswith("/api/v4/secrets/"):
            name = unquote(path.removeprefix("/api/v4/secrets/"))
            where = query if method == "GET" else body
            folder = self.secrets.setdefault(where.get("secretPath"), {})
            if method == "GET":
                return (200, {"secret": {"secretValue": folder[name]}}) if name in folder else (404, {})
            if method == "PATCH" and name not in folder:
                return 404, {}
            if method in ("PATCH", "POST"):
                if not self.lose_writes:
                    folder[name] = body["secretValue"]
                return 200, {}
            if method == "DELETE":
                return (200, folder.pop(name) and {}) if name in folder else (404, {})
        return 404, {}
