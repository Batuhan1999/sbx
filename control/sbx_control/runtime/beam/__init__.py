"""Beam (beam.cloud): hosted sandboxes, through Beam's Gateway HTTP API
(api.py; no SDK). Hosted, like E2B Cloud and Boat: it has no host part, and
the registry scopes it to this installation (Scoped).

What sbx asks for, as Beam has it:

- A sandbox is a Beam container made from a stub (its image, size, env and
  lifetime): POST /stubs, then POST /pods. Its id is Beam's container id.
- Beam keeps no labels on a container, so a sandbox's metadata (sbx's
  installation id and names) is a file in the sandbox (META_FILE), kept in
  memory and read back once after a restart. A container without that file
  is not sbx's.
- Commands and files: sandbox.py. Templates and snapshots: build.py (images,
  named in a file on this server).
- The timeout is Beam's TTL (POST /pods/ID/ttl): when it passes, Beam
  deletes the sandbox, with its files.
- NO PAUSE. Beam ends a sandbox or keeps it running; a memory snapshot
  restores into a new sandbox with another id and other URLs, which is not a
  resume. capabilities.pause is False and pause() refuses: sbx never
  pretends a Beam sandbox is paused.
- Previews: POST /pods/ID/ports/expose answers the port's URL
  (https://<id>-<port>.app.beam.cloud). Beam's URLs are PUBLIC: whoever has
  one reaches the port without sbx's gate. sbx never shows it, but an app
  that echoes its Host header does.
- No per-sandbox deny rules (Beam has an allow list only) and no route to
  this host's private network: egress_rules and private_networks are False.

Nothing here has run against a live Beam account: api.py says what was seen
of the live service, and docs/howto/beam.md lists what the owner checks with
a real token."""
from __future__ import annotations

import datetime
import hashlib
import json
import threading
import time
import urllib.parse

from ... import howto, paths
from ..base import (HOSTNAME, RUNNING, Backend, Capabilities, Failed, Info, LimitSpec, Limits, NotRunning,
                    SettingField, TemplateGone, limit_settings)
from ..hosted import PerSandbox, hosted_status
from .api import DEFAULT_API_URL, Api, BeamError
from .build import START, Builds, Templates
from .sandbox import Handle

KIND = "beam"
API_KEY = "BEAM_API_KEY"
API_URL_KEY = "BEAM_API_URL"
PREVIEW_DOMAIN = "app.beam.cloud"   # where Beam serves exposed ports: <id>-<port>.app.beam.cloud
INGRESS_NAME = "sbx-ingress"        # any name under that wildcard; nginx asks by each port's own name (SNI)
META_FILE = "/etc/sbx/beam-meta.json"
TEMPLATE_KEY = "sbx_template"
FOREIGN_SECONDS = 300               # how long a container without META_FILE is taken as not sbx's
IDLE = ["tail", "-f", "/dev/null"]  # a sandbox's one process, as Beam's SDK starts it
SOURCE = "your Beam workspace"
DAY = 86400
# Beam publishes one size ceiling (the Shared CPU tier: 8 vCPUs, 16 GiB;
# docs.beam.cloud/v2/sandbox/configuration.md, read 2026-11-19) and no
# number for sandboxes at once or the longest TTL (-1 is "never"): those two
# are the admin's own caps.
LIMITS = (
    LimitSpec("timeout_seconds", "BEAM_MAX_TIMEOUT", DAY, "Longest run", "seconds", 300, 30 * DAY, 300),
    LimitSpec("cpu", "BEAM_MAX_CPU", 8, "vCPUs", "", 1, 64, 1),
    LimitSpec("memory_mb", "BEAM_MAX_MEMORY_MB", 16 * 1024, "Memory", "MiB", 1024, 256 * 1024, 1024),
    LimitSpec("sandboxes", "BEAM_MAX_SANDBOXES", 10, "Sandboxes at once", "", 1, 2000, 1),
)
SETTINGS = (
    SettingField(API_KEY, "API token", secret=True, help="From Beam's dashboard: Settings, API keys."),
    SettingField(API_URL_KEY, "API URL", default=DEFAULT_API_URL, optional=True, kind="url", rekey=True, hidden=True,
                 help="Only for another Beam gateway."),
    *limit_settings(LIMITS),
)
NO_PAUSE = ("Beam can't pause a sandbox: it runs until its timeout or until it is deleted, and both delete its "
            "files. Snapshot it to keep them, or run sandboxes that pause on another runtime.")


def _when(text) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")) if text else None
    except ValueError:
        return None


class Beam(Builds, Backend):
    kind = KIND
    title = "Beam"
    help_url = howto.BEAM_KEY
    capabilities = Capabilities(snapshots=True, private_networks=False, egress_rules=False, image_import=True,
                                pause=False)
    internal_suffix = PREVIEW_DOMAIN
    credential_keys = (API_KEY, API_URL_KEY, *(s.key for s in LIMITS))
    secret_key = API_KEY
    settings = SETTINGS

    def __init__(self):
        self._key, self._url = "", DEFAULT_API_URL
        self._limits = Limits.read(LIMITS, {}, SOURCE)
        self.transport = None      # a stand-in for the HTTP client (tests)
        self.index_path = None     # the templates file, when not the installation's own (tests)
        self._meta: dict = {}      # sandbox id -> its metadata, as written at create or read back
        self._foreign: dict = {}   # container id -> when it was found without META_FILE
        self._stubs: dict = {}     # sandbox id -> its stub id
        self._urls: PerSandbox = PerSandbox()   # (sandbox id, port) -> the port's host at Beam
        self._object_id = ""
        self._lock = threading.Lock()

    # Its connection

    def configure(self, secrets):
        key = secrets.get(API_KEY, "")
        if not key:
            raise SystemExit(f"missing platform secret: {API_KEY} (/etc/sbx/runtime.env), an API token from Beam's "
                             f"dashboard: sbx-admin runtime add {KIND} reads it from stdin")
        url = secrets.get(API_URL_KEY) or DEFAULT_API_URL
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not HOSTNAME.fullmatch(parts.hostname or ""):
            raise SystemExit(f"{API_URL_KEY}={url!r} in /etc/sbx/runtime.env is not an https URL such as "
                             f"{DEFAULT_API_URL}")
        self._key, self._url = key, url.rstrip("/")
        self._limits = Limits.read(LIMITS, secrets, SOURCE)

    def api(self) -> Api:
        if not self._key:
            raise Failed(f"Beam is not configured: no {API_KEY} (sbx-admin runtime add {KIND})")
        return Api(self._url, self._key, self.transport)

    def index(self) -> Templates:
        return Templates(self.index_path or paths.state_dir() / "runtime-beam" / "templates.json")

    def failure(self, error):
        return isinstance(error, BeamError)

    def limits(self):
        return self._limits

    # Sandboxes

    def _containers(self) -> list:
        found = self.api().call("GET", "/containers", read=True).get("containers") or []
        return [c for c in found if isinstance(c, dict) and c.get("containerId")
                and str(c.get("status") or "").lower() != "stopping"]

    def _container(self, sandbox_id: str) -> dict:
        found = next((c for c in self._containers() if c["containerId"] == sandbox_id), None)
        if found is None:
            raise NotRunning(f"Beam has no sandbox {sandbox_id}: it was deleted or its timeout passed")
        return found

    def sandbox_call(self, sandbox_id: str, method: str, rest: str, body=None, **options) -> dict:
        """A call on a sandbox (/pods/ID...). When Beam refuses it and no
        longer lists the sandbox, that is NotRunning; any other failure is
        Beam's own (BeamError)."""
        path = f"/pods/{urllib.parse.quote(sandbox_id, safe='')}{rest}"
        try:
            return self.api().call(method, path, body, **options)
        except BeamError as error:
            if error.unauthorized() or error.transient():
                raise
            gone = error.missing()
            if not gone:
                try:
                    gone = not any(c["containerId"] == sandbox_id for c in self._containers())
                except Failed:
                    gone = False
            if gone:
                self._forget(sandbox_id)
                raise NotRunning(f"Beam sandbox {sandbox_id} is not running: {error}") from error
            raise

    def _forget(self, sandbox_id: str) -> None:
        with self._lock:
            self._meta.pop(sandbox_id, None)
            self._stubs.pop(sandbox_id, None)
            self._foreign.pop(sandbox_id, None)
        self._urls.drop(sandbox_id)

    def _object(self) -> str:
        """The code object a stub names: an empty archive, as Beam's SDK
        uploads for a sandbox that syncs no files."""
        if self._object_id:
            return self._object_id
        archive = b"PK\x05\x06" + bytes(18)   # a zip with no entries
        digest = hashlib.sha256(archive).hexdigest()
        api = self.api()
        try:
            found = api.call("GET", f"/objects/{digest}", read=True)
        except BeamError as error:
            if error.status != 200:
                raise
            found = {}
        if not (found.get("exists") and found.get("objectId")):
            found = api.call("POST", "/objects", {"objectMetadata": {"name": digest, "size": len(archive)},
                                                  "hash": digest, "size": len(archive), "overwrite": True,
                                                  "supportsPutHeaders": True})
            if found.get("presignedUrl"):
                status, _ = api.send("PUT", found["presignedUrl"], headers=found.get("putHeaders") or {},
                                     data=archive)
                if status >= 400:
                    raise BeamError(f"Beam's storage answered {status} to the stub's empty archive", status=status)
        self._object_id = found.get("objectId") or ""
        return self._object_id

    def _start(self, image: str, *, cpu: int, memory_mb: int, timeout: int, envs: dict | None) -> tuple[str, str]:
        """A new container from the image: (its id, its stub's id)."""
        api = self.api()
        stub = api.call("POST", "/stubs", {
            "objectId": self._object(), "imageId": image, "stubType": "sandbox", "name": "sandbox", "appName": "sbx",
            "cpu": int(cpu) * 1000, "memory": int(memory_mb), "keepWarmSeconds": int(timeout), "forceCreate": True,
            "entrypoint": IDLE, "env": [f"{k}={v}" for k, v in (envs or {}).items()]})
        if not stub.get("stubId"):
            raise Failed("Beam made a stub without an id")
        made = api.call("POST", "/pods", {"stubId": stub["stubId"]}, timeout=300)
        if not made.get("containerId"):
            raise Failed("Beam made a sandbox without an id")
        with self._lock:
            self._stubs[made["containerId"]] = made.get("stubId") or stub["stubId"]
        return made["containerId"], self._stubs[made["containerId"]]

    def create(self, template, *, timeout, envs=None, metadata=None, deny_out=None, on_timeout=None,
               auto_resume=None):
        """A new sandbox from the template's image, at the template's size.
        on_timeout and auto_resume have no say: at its timeout Beam deletes it."""
        kept = self.index().get(template)
        if kept is None:
            raise TemplateGone(template, f"Beam has no image for the template {template}")
        meta = {k: str(v) for k, v in (metadata or {}).items() if v is not None}
        meta[TEMPLATE_KEY] = template
        try:
            sandbox_id, _ = self._start(kept["image"], cpu=kept.get("cpu") or 1,
                                        memory_mb=kept.get("memory_mb") or 1024, timeout=timeout, envs=envs)
        except BeamError as error:
            if error.status == 200 and self._image_gone(kept["image"]):
                self.index().drop(template)
                raise TemplateGone(template, f"Beam no longer has the image of {template}") from error
            raise
        handle = Handle(self, sandbox_id)
        try:
            handle.files.write(META_FILE, json.dumps(meta), user="root")
            if kept.get("start"):
                handle.commands.run(START, user="root", cwd="/", background=True).disconnect()
        except Exception:
            try:
                self.kill(sandbox_id)   # else it runs, and bills, until its timeout
            except Exception:
                pass
            raise
        with self._lock:
            self._meta[sandbox_id] = {**meta, "cpu": kept.get("cpu"), "memory_mb": kept.get("memory_mb")}
        return handle

    def _image_gone(self, image: str) -> bool:
        try:
            return self.api().call("POST", "/images/verify-build", {"imageId": image}).get("exists") is False
        except Failed:
            return False

    def _stub_of(self, sandbox_id: str) -> str:
        """The sandbox's stub id, which exposing a port and saving an image name (asked once: connect)."""
        if sandbox_id not in self._stubs:
            found = self.sandbox_call(sandbox_id, "POST", "/connect", {})
            with self._lock:
                self._stubs[sandbox_id] = found.get("stubId") or ""
        return self._stubs[sandbox_id]

    def connect(self, sandbox_id, *, timeout=None):
        """The running sandbox (Beam has none paused to resume); timeout renews its TTL."""
        self._stubs.pop(sandbox_id, None)
        self._stub_of(sandbox_id)
        if timeout is not None:
            self.set_timeout(sandbox_id, timeout)
        return Handle(self, sandbox_id)

    def handle(self, sandbox_id):
        """A look: it asks Beam nothing and never waits for a sandbox that is starting."""
        return Handle(self, sandbox_id, ready=0)

    def _meta_of(self, sandbox_id: str) -> dict:
        """The sandbox's metadata: kept, else read from META_FILE; {} for a
        container that has none (another tool's, or Beam refused the look)."""
        if sandbox_id in self._meta:
            return self._meta[sandbox_id]
        if time.monotonic() - self._foreign.get(sandbox_id, -FOREIGN_SECONDS) < FOREIGN_SECONDS:
            return {}
        try:
            found = json.loads(Handle(self, sandbox_id, ready=0).files.read(META_FILE))
        except NotRunning:
            return {}
        except (Failed, ValueError) as error:
            if isinstance(error, BeamError) and error.status != 200:
                raise   # Beam did not answer: not a reason to call it someone else's
            found = None
        with self._lock:
            if isinstance(found, dict):
                self._meta[sandbox_id] = found
            else:
                self._foreign[sandbox_id] = time.monotonic()
        return found if isinstance(found, dict) else {}

    def _info(self, container: dict) -> Info:
        sandbox_id = container["containerId"]
        if container.get("stubId"):
            self._stubs.setdefault(sandbox_id, container["stubId"])
        meta = self._meta_of(sandbox_id)
        labels = {k: v for k, v in meta.items() if k not in ("cpu", "memory_mb")}
        return Info(sandbox_id=sandbox_id, metadata=labels, state=RUNNING, template_id=meta.get(TEMPLATE_KEY, ""),
                    started_at=_when(container.get("startedAt") or container.get("scheduledAt")),
                    cpu_count=meta.get("cpu"), memory_mb=meta.get("memory_mb"))

    def sandboxes(self, metadata=None, states=None):
        """Running ones only: Beam has no paused sandbox. A container without sbx's metadata is not listed."""
        if states is not None and RUNNING not in states:
            return []
        containers = self._containers()
        with self._lock:   # what it kept of containers Beam no longer lists goes (whoever ended them)
            gone = (set(self._meta) | set(self._stubs) | set(self._foreign)) - {c["containerId"] for c in containers}
        for sandbox_id in gone:
            self._forget(sandbox_id)
        found = [self._info(c) for c in containers]
        return [i for i in found if i.metadata and all(i.metadata.get(k) == v for k, v in (metadata or {}).items())]

    def info(self, sandbox_id):
        return self._info(self._container(sandbox_id))

    def kill(self, sandbox_id):
        self._forget(sandbox_id)
        try:
            self.api().call("POST", f"/containers/{urllib.parse.quote(sandbox_id, safe='')}/stop")
        except BeamError as error:
            if error.missing():
                return False
            if error.status != 200 or any(c["containerId"] == sandbox_id for c in self._containers()):
                raise   # Beam still lists it: it was not stopped
            return False   # Beam has none of that id (any more)
        return True

    def pause(self, sandbox_id):
        self._container(sandbox_id)   # NotRunning for one that is gone
        raise Failed(NO_PAUSE)

    def why_busy(self, memory_mb):
        return NO_PAUSE

    def set_timeout(self, sandbox_id, seconds):
        self.sandbox_call(sandbox_id, "POST", "/ttl", {"ttl": int(seconds)})

    # Previews

    @property
    def ingress(self) -> str:
        return f"https://{INGRESS_NAME}.{PREVIEW_DOMAIN}"

    def _expose(self, sandbox_id: str, port: int) -> str:
        found = self.sandbox_call(sandbox_id, "POST", "/ports/expose", {"stubId": self._stub_of(sandbox_id),
                                                                        "port": port})
        host = urllib.parse.urlsplit(found.get("url") or "").hostname or ""
        if not host.endswith("." + PREVIEW_DOMAIN):
            raise Failed(f"Beam exposed port {port} of sandbox {sandbox_id} at {host or 'no URL'}, not under "
                         f"{PREVIEW_DOMAIN}, where sbx sends its previews")
        return host

    def upstream_host(self, port, sandbox_id):
        """The port's host at Beam, as exposing it answered (exposed on its first request, then kept)."""
        return self._urls.get_or_fetch((sandbox_id, int(port)), lambda: self._expose(sandbox_id, int(port)), 10)

    # The settings page and the checks

    def readiness(self, facts):
        from ... import vault
        from ...connections.base import Check
        return [Check("Beam configured", bool(vault.runtime((API_KEY,)).get(API_KEY)), "a Beam API token",
                      f"Add it: sbx-admin runtime add {KIND} (the token on stdin).", help_url=self.help_url)]

    def _account(self) -> dict:
        found = self.api().call("POST", "/auth/authorize", {}, read=True)
        return {"workspace": found.get("workspaceId") or ""}

    def status(self):
        from ...connections.base import Field
        return hosted_status(self, configured=bool(self._key), account=self._account,
                             fields=(Field("api_url", "API URL", value=self._url, kind="url"),))

    def test(self):
        from ...connections.base import Check
        try:
            facts = self._account()
            count = len(self._containers())
        except Exception as error:
            return [Check("Beam API", False, f"{type(error).__name__}: {error}",
                          f"check {API_KEY} in /etc/sbx/runtime.env (sbx-admin runtime add {KIND})",
                          help_url=self.help_url)]
        return [Check("Beam API", True, f"answers; workspace {facts.get('workspace') or 'unknown'}; "
                                        f"{count} containers running")]
