"""The Infisical SecretStore: a project's folders over its HTTP API, signed
in with the installation's machine identity, and a watch for edits made in
Infisical itself (its webhook, or a poll: infisical_api.py).

A namespace is a folder: the installation's values in /_sbx/installation,
a repo's in /repos/<owner>/<repo>, a person's in /_sbx/personal/<hash>,
sealed payloads in /_sbx/objects, anything else in /_sbx/<namespace>.

  python -m sbx_control.secret_store.infisical setup PORT ADMIN_EMAIL
      bootstrap the Infisical the installer started on this host, once
"""
from __future__ import annotations

import gzip
import hashlib
import json
import pathlib
import re
import secrets as pysecrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import paths, settings, state
from .base import BOOTSTRAP, OBJECTS, StoreError, parse_scope
from .local import Local

PREFIX = "infisical:v1:"   # a reference into Infisical: PREFIX + the payload's secret name
CACHE_SECONDS = 60
OBJECT_SECONDS = 10
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def env_file() -> pathlib.Path:
    """The keys of the Infisical this host runs; present once infisical/enable.sh ran."""
    return paths.etc() / "infisical.env"


def admin_file() -> pathlib.Path:
    """Break-glass sign-in to its UI (setup writes it)."""
    return paths.etc() / "infisical-admin"


def _dump_file() -> pathlib.Path:
    return paths.etc() / "infisical.sql.gz"


def _compose(*args, input=None):
    """docker compose for the Infisical this host runs, as infisical/enable.sh runs it."""
    env = {"PATH": "/usr/bin:/bin", "INFISICAL_ENV": str(env_file()),
           "INFISICAL_PORT": str(settings.current().store.port)}
    return subprocess.run(["docker", "compose", "-f", str(paths.shipped("infisical") / "compose.yaml"), *args],
                          input=input, capture_output=input is None, check=True, env=env)


def _private(path: pathlib.Path, data: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_bytes(data)


class Infisical:
    name = "infisical"

    def __init__(self, url: str, project: str, environment: str, credentials):
        self.url, self.project, self.environment = url.rstrip("/"), project, environment
        self.credentials = credentials  # () -> (client id, client secret)
        self.watch = self
        self._lock = threading.RLock()
        self._token = {"value": "", "until": 0.0}
        self._cache: dict = {}
        self._folders: set = set()

    def request(self, method: str, path: str, body=None, token: str | None = None):
        url = self.url + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"content-type": "application/json"}
        token = self.access_token() if token is None else token
        if token:
            headers["authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method),
                                        timeout=15) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as error:
            error.close()
            raise StoreError(f"Infisical answered HTTP {error.code} to {method} {urllib.parse.urlsplit(url).path}",
                             status=error.code) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise StoreError(f"Infisical at {self.url}: {error}") from error

    def access_token(self) -> str:
        if self._token["value"] and time.time() < self._token["until"]:
            return self._token["value"]
        client_id, secret = self.credentials()
        if not client_id or not secret:
            raise StoreError("Infisical's machine identity is not configured (sbx-admin secret set INFISICAL_CLIENT_ID)")
        found = self.request("POST", "/api/v1/auth/universal-auth/login", {"clientId": client_id, "clientSecret": secret},
                             token="")
        self._token.update(value=found["accessToken"], until=time.time() + max(60, int(found.get("expiresIn", 600)) - 60))
        return self._token["value"]

    def signed_in(self) -> bool:
        """Signs in again now: the connection's live check."""
        self._token["until"] = 0
        self.access_token()
        return True

    def answers(self) -> bool:
        self.request("GET", "/api/status", token="")
        return True

    def clear_cache(self):
        with self._lock:
            self._cache.clear()

    def _where(self, path: str) -> dict:
        return {"projectId": self.project, "environment": self.environment, "secretPath": path}

    def _ensure(self, path: str):
        current = ""
        for name in path.strip("/").split("/"):
            parent, current = current or "/", current + "/" + name
            if current in self._folders:
                continue
            try:
                self.request("POST", "/api/v1/folders", {"workspaceId": self.project, "environment": self.environment,
                                                         "name": name, "path": parent})
            except StoreError as error:
                if error.status not in (400, 409):  # it exists already
                    raise
            self._folders.add(current)

    def _values(self, path: str, fresh=False, expand=True) -> dict:
        with self._lock:
            cached = self._cache.get((path, expand))
            if cached and not fresh and time.time() - cached[0] < CACHE_SECONDS:
                return dict(cached[1])
        query = urllib.parse.urlencode({**self._where(path), "expandSecretReferences": str(expand).lower()})
        try:
            result = self.request("GET", "/api/v4/secrets?" + query)
        except StoreError as error:
            if error.status != 404:
                raise
            result = {"secrets": []}
        out = {s["secretKey"]: s.get("secretValue", "") for s in result.get("secrets", [])}
        with self._lock:
            self._cache[(path, expand)] = (time.time(), out)
        return dict(out)

    def _write(self, path: str, name: str, value: str, comment: str = ""):
        self._ensure(path)
        body = {**self._where(path), "secretValue": value, "type": "shared", "skipMultilineEncoding": True,
                **({"secretComment": comment} if comment else {})}
        endpoint = "/api/v4/secrets/" + urllib.parse.quote(name, safe="")
        try:
            self.request("PATCH", endpoint, body)
        except StoreError as error:
            if error.status != 404:
                raise
            self.request("POST", endpoint, body)
        self.clear_cache()

    def _delete(self, path: str, name: str) -> bool:
        try:
            self.request("DELETE", "/api/v4/secrets/" + urllib.parse.quote(name, safe=""), self._where(path))
        except StoreError as error:
            if error.status != 404:
                raise
            return False
        finally:
            self.clear_cache()
        return True

    @staticmethod
    def path(namespace: str) -> str:
        if namespace == "":
            return "/_sbx/installation"
        scope = parse_scope(namespace)
        if scope is None:
            return "/_sbx/" + namespace
        repo, person = scope
        if person:
            return "/_sbx/personal/" + hashlib.sha256(f"{repo.lower()}|{person.lower()}".encode()).hexdigest()
        return "/repos/" + repo.lower()

    def get(self, namespace: str, key: str) -> str | None:
        return self.list(namespace).get(key)

    def put(self, namespace: str, key: str, value: str):
        self._write(self.path(namespace), key, value)
        self._saw(namespace, key, value)

    def delete(self, namespace: str, key: str) -> bool:
        try:
            return self._delete(self.path(namespace), key)
        finally:
            self._saw(namespace, key, None)

    def list(self, namespace: str, fresh=False) -> dict[str, str]:
        found = {k: v for k, v in self._values(self.path(namespace), fresh=fresh).items() if NAME_RE.match(k)}
        if not fresh:
            self._note(namespace, found)
        return found

    def seal(self, text: str, key: str) -> str:
        name = "OBJECT_" + hashlib.sha256(key.encode()).hexdigest()
        self._write(self.path(OBJECTS), name, text, comment="sbx: " + key)
        self._saw(OBJECTS, name, text)
        reference = PREFIX + name
        if self.unseal(reference, fresh=True) != text:  # kept remotely before a column may point at it
            raise StoreError("Infisical did not keep the payload; nothing was changed")
        return reference

    def unseal(self, reference: str, fresh=False) -> str:
        name = reference.removeprefix(PREFIX)
        with self._lock:
            cached = self._cache.get(("object", name))
            if cached and not fresh and time.time() - cached[0] < OBJECT_SECONDS:
                return cached[1]
        query = urllib.parse.urlencode({**self._where(self.path(OBJECTS)), "expandSecretReferences": "false"})
        result = self.request("GET", "/api/v4/secrets/" + urllib.parse.quote(name, safe="") + "?" + query)
        try:
            text = result["secret"]["secretValue"]
        except (KeyError, TypeError):
            raise StoreError("Infisical returned an unreadable payload") from None
        with self._lock:
            self._cache[("object", name)] = (time.time(), text)
        return text

    def discard(self, reference: str):
        name = reference.removeprefix(PREFIX)
        try:
            self._delete(self.path(OBJECTS), name)
        finally:
            self._saw(OBJECTS, name, None)

    @staticmethod
    def owns(reference) -> bool:
        return isinstance(reference, str) and reference.startswith(PREFIX)

    # The watch: hashes of what was seen last, per namespace, in the shared
    # key/value table; names read since (and later deleted) count too.

    def _seen(self, namespace: str) -> state.Setting:
        label = "" if namespace == OBJECTS else namespace
        identity = json.dumps([self.url, self.project, self.environment, self.path(namespace), label])
        return state.Setting("infisical_seen:" + hashlib.sha256(identity.encode()).hexdigest(), encoded=True)

    def _known(self, namespace: str) -> state.Setting:
        return state.Setting("infisical_known:" + self._seen(namespace).key, encoded=True)

    @staticmethod
    def _hashes(values: dict) -> dict:
        return {k: hashlib.sha256(v.encode()).hexdigest() for k, v in values.items()}

    def _note(self, namespace: str, values: dict):
        """A read delivers values before the next poll: the poll must still
        notice a later change, or their deletion."""
        self._seen(namespace).setdefault(self._hashes(values))
        known = self._known(namespace)
        with state.db() as conn:
            conn.execute("begin immediate")
            names = sorted(set(known.get([], conn=conn)) | set(values))
            known.set(names, conn=conn)
            conn.commit()

    def _saw(self, namespace: str, name: str, value: str | None):
        """sbx's own write (value) or deletion (None), as the watch's last
        look: only edits made elsewhere come back as changes. sbx tells the
        sandboxes of its own writes itself (sandboxes.deliver_env), and a
        sealed payload (a sandbox's services, a connection) is no value one
        gets, so its echo would only bring every running sandbox an update."""
        seen = self._seen(namespace)
        with state.db() as conn:
            conn.execute("begin immediate")
            found = seen.get(None, conn=conn)
            if found is not None:  # None: no look yet, and the first one reports every name anyway
                if value is not None:
                    found[name] = self._hashes({name: value})[name]
                elif name in self._known(namespace).get([], conn=conn):
                    found[name] = "absent"
                else:
                    found.pop(name, None)
                seen.set(found, conn=conn)
            conn.commit()

    def changes(self, namespace: str) -> tuple[list[str], dict]:
        values = (self._values(self.path(namespace), fresh=True, expand=False) if namespace == OBJECTS
                  else self.list(namespace, fresh=True))
        now = self._hashes(values)
        for name in self._known(namespace).get([]):
            now.setdefault(name, "absent")
        before = self._seen(namespace).get({})
        return sorted(k for k in set(before) | set(now) if before.get(k) != now.get(k)), now

    def acknowledge(self, namespace: str, snapshot: dict):
        self._seen(namespace).set(snapshot)

    # A backup of the Infisical this host runs: its database and its keys.
    # An Infisical elsewhere (no env_file here) backs itself up.

    @staticmethod
    def backup_parts() -> dict[str, tuple[bytes, bool]]:
        if not env_file().exists():
            return {}
        dump = _compose("exec", "-T", "db", "pg_dump", "--clean", "--if-exists", "-U", "infisical", "infisical").stdout
        parts = {"infisical.sql.gz": (gzip.compress(dump), False), "infisical.env": (env_file().read_bytes(), True)}
        if admin_file().exists():
            parts["infisical-admin"] = (admin_file().read_bytes(), True)
        return parts

    @staticmethod
    def restore_parts(parts: dict[str, bytes]):
        if "infisical.env" not in parts:
            return
        _private(env_file(), parts["infisical.env"])
        _private(_dump_file(), parts["infisical.sql.gz"])
        if "infisical-admin" in parts:
            _private(admin_file(), parts["infisical-admin"])

    @staticmethod
    def finish_restore():
        if not (env_file().exists() and _dump_file().exists()):
            return
        _compose("stop", "backend")
        _compose("up", "-d", "--wait", "db")
        _compose("exec", "-T", "db", "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", "infisical", "infisical",
                 input=gzip.decompress(_dump_file().read_bytes()))
        _dump_file().unlink()
        _compose("up", "-d", "--wait")

    def ensure_webhook(self, base_url: str, key: str):
        """Infisical calls base_url/webhooks/infisical, signed with key, when a value changes."""
        endpoint = base_url.rstrip("/") + "/webhooks/infisical"
        query = urllib.parse.urlencode({"projectId": self.project, "environment": self.environment})
        existing = self.request("GET", "/api/v1/webhooks?" + query).get("webhooks", [])
        if any(hook.get("url") == endpoint and hook.get("secretPath") == "/**" and not hook.get("isDisabled")
               for hook in existing):
            return
        self.request("POST", "/api/v1/webhooks", {"projectId": self.project, "environment": self.environment,
                                                  "webhookUrl": endpoint, "webhookSecretKey": key,
                                                  "secretPath": "/**", "type": "general",
                                                  "eventsFilter": [{"eventName": "secrets.modified"}]})


def setup(port: int, admin_email: str):
    """Bootstrap the Infisical on this host once. Resumable: each step is
    checkpointed."""
    s = settings.current().store
    if s.mode == "infisical" and s.project:
        print(f"infisical: set up already (project {s.project})")
        return
    checkpoint = paths.etc() / "infisical-bootstrap.json"
    admin_email = admin_email or "admin@sbx.invalid"
    client = Infisical(f"http://127.0.0.1:{port}", "", "", lambda: ("", ""))

    def call(method, path, body=None, token=None):
        return client.request(method, path, body, token=progress["root"] if token is None else token)
    if checkpoint.exists():
        progress = json.loads(checkpoint.read_text())
    else:
        password = pysecrets.token_urlsafe(24)
        boot = client.request("POST", "/api/v1/admin/bootstrap",
                              {"email": admin_email, "password": password, "organization": "sbx"}, token="")
        progress = {"root": boot["identity"]["credentials"]["token"], "org": boot["organization"]["id"],
                    "email": admin_email, "password": password}
        checkpoint.touch(mode=0o600)
        checkpoint.write_text(json.dumps(progress))

    def step(name, make):
        if name not in progress:
            progress[name] = make()
            checkpoint.write_text(json.dumps(progress))
        return progress[name]
    project = step("project", lambda: call("POST", "/api/v1/projects", {
        "projectName": "sbx", "projectDescription": "Managed sandbox secrets"})["project"]["id"])
    identity = step("identity", lambda: call("POST", "/api/v1/identities", {
        "name": "sbx-control", "organizationId": progress["org"], "role": "member"})["identity"]["id"])
    client_id = step("client_id", lambda: call(
        "POST", f"/api/v1/auth/universal-auth/identities/{identity}", {})["identityUniversalAuth"]["clientId"])
    client_secret = step("client_secret", lambda: call(
        "POST", f"/api/v1/auth/universal-auth/identities/{identity}/client-secrets",
        {"description": "sbx-control"})["clientSecret"])
    step("member", lambda: bool(call("POST", f"/api/v1/projects/{project}/identity-memberships/{identity}",
                                     {"role": "admin"})))
    Local().put(BOOTSTRAP, "INFISICAL_CLIENT_ID", client_id)
    Local().put(BOOTSTRAP, "INFISICAL_CLIENT_SECRET", client_secret)
    # Break-glass only: its UI is not served (ssh -L 8190:127.0.0.1:8190 to use it).
    _private(admin_file(), f"email={progress['email']}\npassword={progress['password']}\n".encode())
    settings.update("store", {"mode": "infisical", "port": port, "project": project, "environment": "dev"},
                    by="infisical setup")
    print(f"infisical: set up; project {project}, admin {progress['email']} (password in {admin_file()})")
    checkpoint.unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["setup"] and len(sys.argv) == 4:
        state.migrate()
        setup(int(sys.argv[2]), sys.argv[3])
    else:
        sys.exit("python -m sbx_control.secret_store.infisical setup PORT ADMIN_EMAIL")
