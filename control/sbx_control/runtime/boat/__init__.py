"""Boat (boat.dev): hosted Ubuntu sandboxes, through Boat's Python SDK
(boat-sdk), which only this package imports. It has no host side
(HostSide), like E2B Cloud.

What sbx asks for, as Boat has it:

- A template is a saved snapshot: build_template replays the TemplateSpec in
  a builder sandbox (from the parent template's snapshot, or Boat's own
  Ubuntu 24.04 for an image step), saves it under the template's name and
  deletes the builder. A Dockerfile step runs through a small interpreter
  (RUN, ENV, WORKDIR, USER, ARG); COPY and ADD are refused.
- Pause is Boat's stop. Boat keeps the disk, not the memory, so every resume
  is a cold boot (cold_resume): sbx re-ships /run/sbx and restarts the
  recipe's processes. The start command is an enabled systemd unit, which
  Boat starts again on every resume.
- Isolation: every sandbox carries labels (sbx_install, and sbx_template,
  since Boat lists no template), listed with label filters; templates and
  snapshots are named with the installation's prefix (runtime.template_name).
- Previews: a port answers only with its own token. The backend exposes a
  port the first time the gate asks for it (private, never public; relayed
  from loopback when the app listens only there), keeps its token, and the
  gate hands it to nginx, which appends it to the request's Cookie
  (upstream_token_cookie). Boat's own desktop (desktop = "provider") replaces
  sbx's noVNC: Boat keeps port 6080 for itself.
- No per-sandbox egress rules and no route to this host's private network
  (egress_rules, private_networks False): untrusted public repos are refused
  and managed servers are reached through endpoints."""
from __future__ import annotations

import base64
import dataclasses
import datetime
import json
import re
import shlex
import threading
import time
import urllib.parse
import urllib.request
import uuid

from ..base import (FAILED, command_name, PAUSED, RUNNING, Busy, Capabilities, CommandFailed, Failed, HostSide, Info, LimitRefused, Limits,
                    NoRoom, NotRunning, Preset, SettingField, Snapshot, TemplateGone, TemplateSpec, Timeout, HOSTNAME)

KIND = "boat"
API_KEY = "BOAT_API_KEY"
API_URL_KEY = "BOAT_API_URL"
ORG_KEY = "BOAT_ORG"
DEFAULT_API_URL = "https://boat.dev/api/v1"
HOSTED_DOMAIN = "on.boat.dev"   # where Boat serves a sandbox's exposed ports: <subdomain>-<port>.on.boat.dev
INGRESS_NAME = "sbx-ingress"    # any name under that wildcard; nginx asks by each sandbox's own name (SNI)
COOKIE = "_port_auth"           # the cookie a private port's token goes in (docs.boat.dev/hosting)
INSTALL_LABEL, TEMPLATE_LABEL, BUILD_LABEL = "sbx_install", "sbx_template", "sbx_build"
RELOOK_SECONDS = 3     # how often a port nothing answered on yet is looked at again
# Boat's own ports in every sandbox (docs.boat.dev/hosting): never a preview.
RESERVED_PORTS = frozenset({22, 6080, 8090, 8911, *range(47984, 48011)})
# States as Boat's API names them (docs.boat.dev/api/v1, Sandbox lifecycle).
UP = ("init", "provisioning", "provisioned", "cloning", "ready", "idle", "running")
READY = ("ready", "idle", "running")
STOPPED = ("archiving", "archived")
GONE = ("cancelled",)
HOME = "/home/user"
TEMPLATE_FILE = "/etc/sbx/template.sh"   # a template's env and workdir, sourced by every command
START_UNIT = "sbx-start"
SYNC_SECONDS = 600       # the longest command Boat's API waits for; longer ones run detached
FILE_LIMIT = 5 * 1024 * 1024
POLL_SECONDS = 2

# The machine classes (https://docs.boat.dev/machines.md, read 2026-11-17):
# name, vCPUs, memory and disk in MiB (Boat's GB as GiB). A sandbox gets the
# smallest one its size fits.
MACHINES = (("small", 2, 4 * 1024, 51 * 1024), ("default", 4, 8 * 1024, 51 * 1024),
            ("large", 8, 16 * 1024, 111 * 1024), ("xlarge", 16, 32 * 1024, 251 * 1024))
MONTH = 30 * 86400   # the longest auto-stop on a paid plan

# The limits, by Limits field: the runtime.env key and its default, the $20
# plan's (PRESETS).
LIMITS = {"timeout_seconds": ("BOAT_MAX_TIMEOUT", MONTH), "cpu": ("BOAT_MAX_CPU", 8),
          "memory_mb": ("BOAT_MAX_MEMORY_MB", 16 * 1024), "disk_mb": ("BOAT_MAX_DISK_MB", 111 * 1024),
          "sandboxes": ("BOAT_MAX_SANDBOXES", 100)}
LIMIT_FORM = {"timeout_seconds": ("Longest run", "seconds", 300, MONTH, 300),
              "cpu": ("vCPUs", "", 2, 16, 2),
              "memory_mb": ("Memory", "MiB", 4096, 32768, 4096),
              "disk_mb": ("Disk", "MiB", 51 * 1024, 251 * 1024, 1024),
              "sandboxes": ("Sandboxes at once", "", 1, 2000, 1)}
SETTINGS = (
    SettingField(API_KEY, "API key", secret=True, help="From Boat's dashboard: API keys (boat_…)."),
    SettingField(API_URL_KEY, "API URL", default=DEFAULT_API_URL, optional=True, rekey=True,
                 help="Only for another Boat API."),
    SettingField(ORG_KEY, "Organization", optional=True, help="Bill an organization's wallet instead of yours."),
    *(SettingField(key, LIMIT_FORM[field][0], default=str(default), kind="int", limit=field, unit=LIMIT_FORM[field][1],
                   min=LIMIT_FORM[field][2], max=LIMIT_FORM[field][3], step=LIMIT_FORM[field][4])
      for field, (key, default) in LIMITS.items()),
)
# Boat's plans as limits, from https://docs.boat.dev/pricing.md,
# https://docs.boat.dev/limits.md and https://docs.boat.dev/machines.md (read
# 2026-11-17). Every paid plan: auto-stop up to 30 days. $20: 100 sandboxes
# at once, sizes up to large (8 vCPUs, 16 GB, 111 GB). $100: 300 at once and
# xlarge (16 vCPUs, 32 GB, 251 GB). $500: 1,000. $2000: 2,000. The trial:
# 2 at once, auto-stop at most 2 hours, the default size (4 vCPUs, 8 GB), and
# no saved snapshots: Boat answered a save on a trial account (2026-11-17)
# "Saved snapshots are not available during the free trial. End the trial
# (first payment) to unlock them." sbx's templates are saved snapshots, so a
# trial account is accepted (sandboxes from templates built once it pays)
# and a template build on it is refused before it starts a builder.
_PLAN_SOURCE = "https://docs.boat.dev/pricing.md"


TRIAL_NOTE = "Trial: templates need the first payment (Boat saves no snapshots during the trial)."


def _plan(name, title, sandboxes, largest, timeout=None, note=""):
    _, cpu, memory, disk = next(m for m in MACHINES if m[0] == largest)
    return Preset(name, title, {"BOAT_MAX_TIMEOUT": str(timeout or MONTH), "BOAT_MAX_CPU": str(cpu),
                                "BOAT_MAX_MEMORY_MB": str(memory), "BOAT_MAX_DISK_MB": str(disk),
                                "BOAT_MAX_SANDBOXES": str(sandboxes)}, _PLAN_SOURCE, note=note)


PRESETS = (_plan("trial", "Trial", 2, "default", timeout=2 * 3600, note=TRIAL_NOTE), _plan("20", "$20", 100, "large"),
           _plan("100", "$100", 300, "xlarge"),
           _plan("500", "$500", 1000, "xlarge"), _plan("2000", "$2000", 2000, "xlarge"))
TRIAL_REFUSAL = ("Boat's free trial saves no snapshots (\"Saved snapshots are not available during the free "
                 "trial\"), and sbx's templates are saved snapshots: make the first payment in Boat's dashboard "
                 "(Billing), then build again.")
KEY_WARN_DAYS = 14
PLAN_CACHE_SECONDS = 300
BUILD_SECONDS = 4 * 3600   # the longest a builder runs; one older is an orphan
ENV_DIR = "/tmp/.sbx-env"  # where a command's env waits for it (a new machine's /tmp is empty)


def _limits(secrets: dict) -> Limits:
    found = {}
    for field, (key, default) in LIMITS.items():
        value = secrets.get(key) or default
        try:
            found[field] = int(value)
        except (TypeError, ValueError):
            raise SystemExit(f"{key}={value} in /etc/sbx/runtime.env is not a whole number") from None
    try:
        return Limits(**found, source="your Boat plan", keys={f: key for f, (key, _) in LIMITS.items()})
    except ValueError as error:
        raise SystemExit(f"{LIMITS['timeout_seconds'][0]} in /etc/sbx/runtime.env: {error}") from None


def machine(cpu: int | None, memory_mb: int | None, disk_mb: int | None) -> str:
    """The smallest machine class a size fits; LimitRefused when none does."""
    for name, most_cpu, most_memory, most_disk in MACHINES:
        if (cpu or 0) <= most_cpu and (memory_mb or 0) <= most_memory and (disk_mb or 0) <= most_disk:
            return name
    raise LimitRefused(f"Boat has no machine with {cpu} vCPUs, {memory_mb} MiB and {disk_mb} MiB of disk; its largest "
                       "(xlarge) has 16 vCPUs, 32 GB and 251 GB", field="cpu", asked=cpu, allowed=16)


SAVED_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def saved_name(name: str) -> str:
    """A template's or snapshot's name as a Boat saved-snapshot name (1-63
    lowercase letters, digits or dashes): as is when it is one (sbx's
    template names are), else made one, the same every time."""
    if SAVED_NAME.fullmatch(name):
        return name
    import hashlib
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return (re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:54].strip("-") or "s") + "-" + digest


def _given(**fields) -> dict:
    """The fields that have a value: a request model sends a field set to
    None as null (Boat's ttlSeconds null: auto-stop off)."""
    return {k: v for k, v in fields.items() if v is not None}


def _installation() -> str:
    from .. import installation_id
    return installation_id()


@dataclasses.dataclass(frozen=True)
class Exposed:
    """A port as Boat exposes it: its host and token, whether Boat relays it
    from loopback, whether something answered on it (settled) and when it
    was last looked at."""
    host: str
    token: str
    relay: bool
    settled: bool
    checked: float


# Errors


def _body(error) -> dict:
    try:
        found = json.loads(getattr(error, "body", None) or "{}")
        return found if isinstance(found, dict) else {}
    except ValueError:
        return {}


def code(error) -> str:
    """Boat's error code ("" for anything that is not an answer of Boat's)."""
    body = _body(error)
    return str(body.get("code") or (body.get("error") or {}).get("code") or "")


def status(error) -> int | None:
    found = getattr(error, "status", None)
    return found if isinstance(found, int) else None


def _message(error) -> str:
    body = _body(error)
    return str(body.get("message") or (body.get("error") or {}).get("message") or getattr(error, "reason", "") or error)


def _retryable(error) -> bool:
    details = ((_body(error).get("error") or {}).get("details") or {})
    return (code(error) in ("sandbox_starting", "sandbox_resuming", "idempotency_in_progress")
            or (code(error) == "sandbox_direct_failed" and (details.get("retryable") or _message(error) == "sandbox_restoring")))


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


def _gone_snapshot(error: Failed) -> bool:
    """Whether Boat answered that a saved snapshot is not there (deleted, or
    its data expired): the template is gone."""
    cause = error.__cause__
    return cause is not None and (code(cause) in ("snapshot_not_found", "snapshot_data_expired")
                                  or status(cause) == 404)


def _failed(error) -> Failed:
    return Failed(f"Boat answered {status(error)} {code(error) or ''}: {_message(error)}".replace("  ", " "))


# Commands as Boat runs them


class ShellWord(str):
    """A value written as the shell word that makes it: a Dockerfile's ENV or
    ARG, whose $VAR and ${VAR} expand from the env before it (earlier ENV and
    ARG, the base image's) as Docker expands them. Never a command
    substitution: shell_word escapes those."""


UNQUOTED_LITERAL = set(";&|<>()*?[]~!{}")   # shell syntax outside quotes, plain text in a Dockerfile's value


def shell_word(raw: str, *, quoted: bool = False) -> ShellWord:
    """raw as a ShellWord: as written (its quotes kept, $VAR expanding), or,
    quoted, the whole of it in double quotes (ENV's old KEY value form).
    Backticks and $( are escaped outside single quotes, and outside any
    quotes the characters that are shell syntax but plain text to Docker
    (UNQUOTED_LITERAL; ${...} stays an expansion)."""
    out, single, double, param = [], False, quoted, 0
    text = raw.replace("\\", "\\\\").replace('"', '\\"') if quoted else raw
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and not single and i + 1 < len(text):
            out.append(text[i:i + 2])
            i += 2
            continue
        if not single and text.startswith("${", i):
            out.append("${")
            param, i = param + 1, i + 2
            continue
        if c == "}" and param:
            out.append(c)
            param, i = param - 1, i + 1
            continue
        if c == "'" and not double:
            single = not single
        elif c == '"' and not single:
            double = not double
        elif not single and (c == "`" or text.startswith("$(", i)):
            out.append("\\")
        elif not single and not double and c in UNQUOTED_LITERAL:   # Docker takes them as they are
            out.append("\\")
        out.append(c)
        i += 1
    word = "".join(out)
    return ShellWord(f'"{word}"' if quoted else (word or "''"))


def export_line(key: str, value) -> str:
    """export KEY=VALUE for a shell: a ShellWord as written, anything else quoted."""
    return f"export {key}={value if isinstance(value, ShellWord) else shlex.quote(str(value))}\n"


def wrap(cmd: str, *, user: str | None = None, env_file: str | None = None, cwd: str | None = None) -> str:
    """cmd as one shell command for Boat's API, which runs as `user` in its
    home and takes no env or user: the template's env and workdir first
    (TEMPLATE_FILE), then the command's env from env_file (written through
    the files API, read once and removed: values never appear in the command
    Boat keeps), then cwd, whose failure stops it; another user through sudo
    (the env file handed over as theirs, 0600)."""
    where = f"cd {shlex.quote(cwd)} || exit 1; " if cwd else 'cd "${SBX_TEMPLATE_WORKDIR:-$HOME}" 2>/dev/null; '
    other = bool(user and user != "user")
    # Another user can't read ENV_DIR (the default user's, 0700): its copy goes in /tmp, 0600, its own.
    mine = f"/tmp/.sbx-env-{env_file.rsplit('/', 1)[1]}.{user}" if env_file and other else env_file
    load = f"{{ . {mine} && rm -f {mine}; }} || exit 1; " if env_file else ""
    script = f"[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}; {load}{where}{cmd}"
    if not other:
        return f"bash -c {shlex.quote(script)}"
    hand = (f"sudo install -o {shlex.quote(user)} -m 600 {env_file} {mine} && rm -f {env_file} || exit 1; "
            if env_file else "")
    return f"bash -c {shlex.quote(hand + 'exec sudo -H -u ' + shlex.quote(user) + ' bash -c "$1"')} sbx {shlex.quote(script)}"


class _Background:
    """A detached command: it keeps running after disconnect()."""

    def __init__(self, process_id):
        self.process_id, self.stdout, self.stderr, self.exit_code = process_id, "", "", 0

    def disconnect(self):
        return None


class _Commands:
    def __init__(self, backend: "Boat", sandbox_id: str):
        self._backend, self._id = backend, sandbox_id

    def run(self, cmd, *, user=None, envs=None, background=False, timeout=60, cwd=None, on_output=None):
        """Synchronous up to SYNC_SECONDS; a longer (or unbounded) one runs
        detached and is polled; background returns once it started."""
        from boat_sdk.models import CommandRequest
        env_file = self._backend.env_file(self._id, envs) if envs else None
        command = wrap(cmd, user=user, env_file=env_file, cwd=cwd)
        api = self._backend.api()
        if background or not timeout or timeout > SYNC_SECONDS:
            started = self._backend.call(lambda: api.command(self._id, CommandRequest(command=command, detached=True)),
                                         sandbox_id=self._id).actual_instance
            if background:
                return _Background(started.process_id)
            return self._wait(started.process_id, timeout, on_output, cmd)
        result = self._backend.call(lambda: api.command(self._id, CommandRequest(
            command=command, timeout_seconds=max(1, min(SYNC_SECONDS, int(-(-timeout // 1)))))),
            sandbox_id=self._id).actual_instance
        if result.timed_out:
            raise Timeout(f"`{command_name(cmd)}` ran out of its {timeout} s in Boat sandbox {self._id}")
        return self._done(result.stdout or "", result.stderr or "", result.exit_code, cmd)

    def _wait(self, process_id, timeout, on_output, cmd=""):
        api = self._backend.api()
        deadline = time.monotonic() + timeout if timeout else None
        offsets = {"out": 0, "err": 0}
        out, err = [], []
        while True:
            found = self._backend.call(lambda: api.command_status(
                self._id, process_id, stdout_offset=offsets["out"], stderr_offset=offsets["err"]), sandbox_id=self._id)
            out.append(found.stdout or "")
            err.append(found.stderr or "")
            if on_output and found.stdout:
                on_output(found.stdout)
            offsets["out"] = found.stdout_next_offset or offsets["out"] + len((found.stdout or "").encode())
            offsets["err"] = found.stderr_next_offset or offsets["err"] + len((found.stderr or "").encode())
            if not found.running:
                return self._done("".join(out), "".join(err), found.exit_code, cmd)
            if deadline is not None and time.monotonic() > deadline:
                try:
                    api.stop_command(self._id, process_id)
                except Exception:   # it ends with the sandbox
                    pass
                raise Timeout(f"the command ran out of its {timeout} s in Boat sandbox {self._id}")
            if not found.more:
                time.sleep(POLL_SECONDS)

    @staticmethod
    def _done(stdout, stderr, exit_code, cmd=""):
        from types import SimpleNamespace
        if exit_code not in (0, None):
            raise CommandFailed(stdout=stdout, stderr=stderr, exit_code=exit_code, command=cmd)
        return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code or 0)


def _allowed_path(path: str) -> bool:
    """Where Boat's file API reads and writes: under /home/user or /tmp."""
    clean = re.sub(r"/+", "/", path)
    return not re.search(r"(^|/)\.\.(/|$)", clean) and (
        clean.startswith(HOME + "/") or clean.startswith("/tmp/") or not clean.startswith("/"))


class _Files:
    def __init__(self, backend: "Boat", sandbox_id: str, commands: _Commands):
        self._backend, self._id, self._commands = backend, sandbox_id, commands

    def write(self, path, data, *, user=None):
        raw = data.encode() if isinstance(data, str) else bytes(data)
        if _allowed_path(path) and user in (None, "user"):
            return self._put(path, raw)
        staging = f"/tmp/.sbx-upload-{uuid.uuid4().hex}"
        self._put(staging, raw)
        folder = shlex.quote(path.rsplit("/", 1)[0] or "/")
        self._commands.run(f"mkdir -p {folder} && mv -f {staging} {shlex.quote(path)}", user=user, timeout=60)

    def _put(self, path, raw: bytes):
        from boat_sdk.models import FileUrlRequest, FileWriteRequest
        api = self._backend.api()
        if len(raw) <= FILE_LIMIT:
            return self._backend.call(lambda: api.write_file(self._id, FileWriteRequest(
                path=path, content=base64.b64encode(raw).decode(), encoding="base64")), sandbox_id=self._id)
        url = self._backend.call(lambda: api.create_upload_url(self._id, FileUrlRequest(path=path)),
                                 sandbox_id=self._id)
        self._backend.http(url.url, method=url.method or "PUT", data=raw)

    def read(self, path, *, format="text"):
        from boat_sdk.models import FileUrlRequest
        api = self._backend.api()
        if not _allowed_path(path):
            raw = base64.b64decode(self._commands.run(f"base64 -w0 {shlex.quote(path)}", timeout=120).stdout)
        else:
            try:
                found = self._backend.call(lambda: api.read_file(self._id, path, encoding="base64"),
                                           sandbox_id=self._id)
                raw = base64.b64decode(found.content)
            except Failed as error:
                if "file_too_large" not in str(error):
                    raise
                url = self._backend.call(lambda: api.create_download_url(self._id, FileUrlRequest(path=path)),
                                         sandbox_id=self._id)
                raw = self._backend.http(url.url, method="GET")
        return raw if format == "bytes" else raw.decode(errors="replace")


class Handle:
    def __init__(self, backend: "Boat", sandbox_id: str):
        self._backend = backend
        self.sandbox_id = sandbox_id
        self.commands = _Commands(backend, sandbox_id)
        self.files = _Files(backend, sandbox_id, self.commands)

    def kill(self):
        return self._backend.kill(self.sandbox_id)


# The Dockerfile steps Boat can replay


def dockerfile_steps(text: str) -> list:
    """A Dockerfile's instructions as TemplateSpec steps: RUN, ENV, WORKDIR,
    USER and ARG (its default, as ENV for the steps after it); FROM and what
    only describes an image (EXPOSE, LABEL, CMD, ENTRYPOINT, ...) are
    skipped. COPY and ADD are refused: a builder on Boat has no build context."""
    lines, current = [], ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):   # also inside a continuation, as Docker drops them
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        lines.append((current + stripped).strip())
        current = ""
    if current.strip():
        lines.append(current.strip())
    steps = []
    for line in lines:
        word, _, rest = line.partition(" ")
        word, rest = word.upper(), rest.strip()
        if word == "RUN":
            if rest.startswith("["):
                try:
                    rest = shlex.join(json.loads(rest))
                except ValueError:
                    pass
            steps.append(("run", rest))
        elif word == "ENV":
            steps.append(("env", _pairs(rest)))
        elif word == "ARG":
            name, sep, value = rest.partition("=")
            if sep:
                steps.append(("env", {name.strip(): shell_word(value.strip())}))
        elif word == "WORKDIR":
            steps.append(("workdir", rest))
        elif word == "USER":
            steps.append(("user", rest.split(":")[0]))
        elif word in ("COPY", "ADD"):
            raise Failed(f"Boat can't build a Dockerfile's {word} ({line[:80]}): a template on Boat is built in a "
                         "sandbox with no build context. Fetch the file in a RUN step (curl, git) instead.")
    return steps


def _words(text: str) -> list:
    """text split at blanks outside quotes, each word as written (quotes and
    backslashes kept)."""
    words, current, quote, escaped = [], "", "", False
    for c in text:
        if escaped:
            current, escaped = current + c, False
        elif c == "\\" and quote != "'":
            current, escaped = current + c, True
        elif quote:
            current += c
            quote = "" if c == quote else quote
        elif c in "'\"":
            current, quote = current + c, c
        elif c.isspace():
            if current:
                words.append(current)
            current = ""
        else:
            current += c
    return words + ([current] if current else [])


def _pairs(text: str) -> dict:
    """ENV's KEY=VALUE pairs (or the old KEY VALUE form), each value the
    ShellWord that expands it as Docker would."""
    if "=" not in text.split(" ", 1)[0]:
        key, _, value = text.partition(" ")
        return {key: shell_word(value.strip(), quoted=True)}
    found = {}
    for token in _words(text):
        key, _, value = token.partition("=")
        found[key] = shell_word(value)
    return found


class Boat(HostSide):
    kind = KIND
    title = "Boat"
    capabilities = Capabilities(snapshots=True, private_networks=False, egress_rules=False)
    ingress_cleared = ()
    ports = RESERVED_PORTS
    credential_keys = (API_KEY, API_URL_KEY, ORG_KEY, *(key for key, _ in LIMITS.values()))
    secret_key = API_KEY
    settings = SETTINGS
    presets = PRESETS
    upstream_token_header = None
    upstream_token_cookie = COOKIE
    desktop = "provider"
    cold_resume = True
    internal_suffix = HOSTED_DOMAIN

    def __init__(self):
        self._key = ""
        self._url = DEFAULT_API_URL
        self._org = ""
        self._limits = _limits({})
        self._client = None
        self._local = threading.local()
        self.transport = None   # a stand-in for the SDK's HTTP client (tests)
        self.http_open = None   # a stand-in for the signed-URL transfers (tests)
        self._exposed: dict = {}    # (sandbox id, port) -> Exposed
        self._relay: dict = {}      # (sandbox id, port) -> relayed from loopback, as last seen (kept across stops)
        self._env_dirs: set = set()  # sandboxes whose machine has ENV_DIR
        self._plan: tuple = (0.0, "")   # (when, Boat's plan key) as last asked
        self._subdomains: dict = {}  # sandbox id -> its subdomain
        self._lock = threading.Lock()
        self._port_locks: dict = {}

    # The connection

    def configure(self, secrets):
        key = secrets.get(API_KEY, "")
        if not key:
            raise SystemExit(f"missing platform secret: {API_KEY} (/etc/sbx/runtime.env), an API key from Boat's "
                             f"dashboard: sbx-admin runtime add {KIND} reads it from stdin")
        url = secrets.get(API_URL_KEY) or DEFAULT_API_URL
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not HOSTNAME.fullmatch(parts.hostname or ""):
            raise SystemExit(f"{API_URL_KEY}={url!r} in /etc/sbx/runtime.env is not an https URL such as "
                             f"{DEFAULT_API_URL}")
        self._key, self._url, self._org = key, url.rstrip("/"), secrets.get(ORG_KEY, "")
        self._limits = _limits(secrets)
        self._client = None

    def api(self):
        """The SDK's API object over this backend's own connection, one per thread."""
        from boat_sdk import ApiClient, Configuration
        from boat_sdk.api.boat_api import BoatApi
        if not self._key:
            raise Failed(f"Boat is not configured: no {API_KEY} (sbx-admin runtime add {KIND})")
        found = getattr(self._local, "api", None)
        if found is None or getattr(self._local, "key", None) != (self._key, self._url):
            client = ApiClient(Configuration(host=self._url, access_token=self._key))
            if self.transport is not None:
                client.rest_client = self.transport
            found = self._local.api = BoatApi(client)
            self._local.key = (self._key, self._url)
        return found

    def _org_args(self) -> dict:
        return {"org": self._org} if self._org else {}

    def http(self, url: str, *, method: str, data: bytes | None = None) -> bytes:
        """A signed upload or download URL's transfer (no API key: it carries its own)."""
        if self.http_open is not None:
            return self.http_open(url, method, data)
        request = urllib.request.Request(url, data=data, method=method)
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.read()

    def call(self, function, *, sandbox_id: str | None = None, asked: dict | None = None, retry_seconds: float = 180):
        """function's result; Boat's errors as sbx's: a sandbox that has no
        machine, or that Boat no longer has, as NotRunning, a limit as
        LimitRefused or NoRoom, anything else as Failed. Answers Boat says to
        retry (a sandbox still starting or restoring its disk) are retried."""
        deadline = time.monotonic() + retry_seconds
        while True:
            try:
                return function()
            except Failed:
                raise
            except Exception as error:
                if status(error) is None:
                    if self.failure(error):
                        raise Failed(f"Boat did not answer: {type(error).__name__}: {error}") from error
                    raise
                if _retryable(error) and time.monotonic() < deadline:
                    time.sleep(POLL_SECONDS)
                    continue
                found = refused(error, asked)
                if found is not None:
                    raise found from error
                if sandbox_id and (status(error) == 404 or code(error) in (
                        "machine_not_running", "sandbox_not_ready", "snapshot_data_expired")):
                    raise NotRunning(f"Boat sandbox {sandbox_id} is not running: {_message(error)}") from error
                raise _failed(error) from error

    # Previews

    @property
    def ingress(self) -> str:
        return f"https://{INGRESS_NAME}.{HOSTED_DOMAIN}"

    def own_route(self, host, sandbox_id, port):
        return None

    def upstream_host(self, port, sandbox_id):
        """<subdomain>-<port>.on.boat.dev, as exposing the port returned it."""
        kept = self._exposed.get((sandbox_id, int(port)))
        if kept:
            return kept.host
        subdomain = self._subdomains.get(sandbox_id)
        if not subdomain:
            subdomain = self._sandbox(sandbox_id).subdomain or ""
            if subdomain:
                self._subdomains[sandbox_id] = subdomain
        return f"{subdomain or sandbox_id}-{int(port)}.{HOSTED_DOMAIN}"

    def upstream_token(self, sandbox_id, port, *, fetch=True):
        """The port's token, exposing the port on its first request: private
        (never public), relayed from loopback when the app listens only
        there. Until something answers on the port, each request (at most
        every few seconds) looks at how it listens again and exposes it again
        when that changed; once it answers, the exposure is kept until the
        sandbox stops or goes. With nothing listening yet (a resume), the
        relay is as it was last seen: exposing again without it would turn
        Boat's relay off."""
        key = (sandbox_id, int(port))
        kept = self._exposed.get(key)
        if not fetch or (kept and (kept.settled or time.monotonic() - kept.checked < RELOOK_SECONDS)):
            return kept.token if kept else ""
        with self._lock:
            lock = self._port_locks.setdefault(key, threading.Lock())
        with lock:
            kept = self._exposed.get(key)
            if kept and (kept.settled or time.monotonic() - kept.checked < RELOOK_SECONDS):
                return kept.token
            seen = self._listening_on(sandbox_id, int(port))   # True: loopback only, False: reachable, None: nothing
            relay = seen if seen is not None else self._relay.get(key, kept.relay if kept else False)
            if seen is not None:
                self._relay[key] = seen
            if kept and kept.relay == relay:
                self._exposed[key] = Exposed(kept.host, kept.token, relay, seen is not None, time.monotonic())
                return kept.token
            host, token = self._expose(sandbox_id, int(port), relay)
            self._exposed[key] = Exposed(host, token, relay, seen is not None, time.monotonic())
            return token

    def _expose(self, sandbox_id: str, port: int, loopback: bool) -> tuple[str, str]:
        from boat_sdk.models import HostPortRequest
        found = self.call(lambda: self.api().host_port(sandbox_id, HostPortRequest(port=port, public=False,
                                                                                   localhost=loopback)),
                          sandbox_id=sandbox_id)
        parts = urllib.parse.urlsplit(found.url or "")
        token = urllib.parse.parse_qs(parts.query).get("_token", [""])[0]
        if not parts.hostname or not token:
            raise Failed(f"Boat exposed port {port} of sandbox {sandbox_id} without a token; its previews are not "
                         "served without one")
        return parts.hostname, token

    def _listening_on(self, sandbox_id: str, port: int) -> bool | None:
        """How the port listens now: True when only on loopback (Boat then
        relays to it), False when on an address Boat reaches, None when
        nothing listens (or the sandbox did not answer)."""
        try:
            listening = Handle(self, sandbox_id).commands.run(f"ss -Hltn 'sport = :{port}'", timeout=15).stdout
        except (CommandFailed, Timeout, NotRunning):
            return None
        addresses = [line.split()[3].rsplit(":", 1)[0] for line in listening.splitlines() if len(line.split()) > 3]
        if not addresses:
            return None
        return all(a in ("127.0.0.1", "[::1]", "::1") or a.startswith("127.") for a in addresses)

    def desktop_url(self, sandbox_id):
        """A fresh URL of Boat's desktop for the sandbox (it carries a token
        that works for 10 minutes)."""
        for _ in range(30):
            found = self.call(lambda: self.api().desktop(sandbox_id), sandbox_id=sandbox_id)
            if found.desktop_url:
                return found.desktop_url
            time.sleep(POLL_SECONDS)
        raise Failed(f"Boat did not open the desktop of sandbox {sandbox_id}")

    def _forget(self, sandbox_id: str, *, gone: bool = False):
        """What it kept of a sandbox whose machine went (a stop): its ports'
        exposures and its env folder; the relay as last seen stays, unless
        the sandbox itself is gone."""
        for key in [k for k in self._exposed if k[0] == sandbox_id]:
            self._exposed.pop(key, None)
        self._env_dirs.discard(sandbox_id)
        if gone:
            for key in [k for k in self._relay if k[0] == sandbox_id]:
                self._relay.pop(key, None)
        with self._lock:
            for key in [k for k in self._port_locks if k[0] == sandbox_id and not self._port_locks[k].locked()]:
                del self._port_locks[key]

    def listed(self, sandbox_ids: set) -> None:
        """After a full listing: forget what it kept of sandboxes no longer there."""
        for sandbox_id in ({k[0] for k in self._exposed} | {k[0] for k in self._relay}) - set(sandbox_ids):
            self._forget(sandbox_id, gone=True)
        for sandbox_id in set(self._subdomains) - set(sandbox_ids):
            self._subdomains.pop(sandbox_id, None)

    def limits(self):
        return self._limits

    # Sandboxes

    def _sandbox(self, sandbox_id):
        found = self.call(lambda: self.api().get(sandbox_id), sandbox_id=sandbox_id).sandbox
        if found.state in GONE:
            raise NotRunning(f"Boat sandbox {sandbox_id} was cancelled")
        return found

    def _info(self, sandbox) -> Info:
        state = (RUNNING if sandbox.state in UP else PAUSED if sandbox.state in STOPPED else FAILED)
        if sandbox.subdomain:
            self._subdomains[sandbox.id] = sandbox.subdomain
        memory = sandbox.memory_gb * 1024 if sandbox.memory_gb else None
        return Info(sandbox_id=sandbox.id, metadata=dict(sandbox.labels or {}), state=state,
                    template_id=(sandbox.labels or {}).get(TEMPLATE_LABEL, ""), started_at=sandbox.created_at,
                    end_at=sandbox.archive_after if state == RUNNING else sandbox.stopped_at,
                    cpu_count=sandbox.vcpu, memory_mb=memory)

    def create(self, template, *, timeout, envs=None, metadata=None, deny_out=None, on_timeout=None,
               auto_resume=None):
        """A new sandbox from the template's saved snapshot: no account
        secrets of Boat's (noEnv), sbx's metadata as labels."""
        from boat_sdk.models import CreateSandboxRequest
        labels = {k: str(v) for k, v in (metadata or {}).items() if v is not None}
        labels[TEMPLATE_LABEL] = template
        if len(labels) > 20 or any(len(v.encode()) > 256 for v in labels.values()):
            raise Failed("Boat keeps at most 20 labels of 256 bytes each; this sandbox's metadata is larger")
        request = CreateSandboxRequest(var_from=saved_name(template), labels=labels, env=dict(envs or {}) or None,
                                       no_env=True, environment=None, type=None, name=labels.get("sbx"),
                                       **_given(ttl_seconds=timeout), **self._org_args())
        once = str(uuid.uuid4())   # one key for every retry: a lost answer never makes a second sandbox
        try:
            created = self.call(lambda: self.api().create(idempotency_key=once, create_sandbox_request=request),
                                asked={"timeout_seconds": timeout})
        except Failed as error:
            if _gone_snapshot(error):
                raise TemplateGone(template, f"Boat has no saved snapshot {saved_name(template)}") from error
            raise
        sandbox_id = created.sandbox.id
        self._ready(sandbox_id)
        return Handle(self, sandbox_id)

    def _ready(self, sandbox_id: str, seconds: float = 600):
        """Wait until Boat says the sandbox takes commands."""
        deadline = time.monotonic() + seconds
        while True:
            found = self._sandbox(sandbox_id)
            if found.state in READY:
                return found
            if found.state == "error":
                raise Failed(f"Boat sandbox {sandbox_id} failed to start: {found.error or 'error'}")
            if found.state == "archived":
                raise NotRunning(f"Boat sandbox {sandbox_id} stopped while starting")
            if time.monotonic() > deadline:
                raise Timeout(f"Boat sandbox {sandbox_id} was not ready after {int(seconds)} s ({found.state})")
            time.sleep(POLL_SECONDS)

    def connect(self, sandbox_id, *, timeout=None):
        """The running sandbox, resuming a stopped one (a cold boot: its
        machine is new, its exposed ports' tokens are asked again)."""
        from boat_sdk.models import ResumeRequest, UpdateSandboxRequest
        found = self._sandbox(sandbox_id)
        if found.state == "archiving":
            found = self._settle(sandbox_id)
        if found.state in ("archived", "error"):
            self._forget(sandbox_id)
            # No ttlSeconds without a timeout: null would turn its auto-stop off for good.
            self.call(lambda: self.api().resume(sandbox_id, ResumeRequest(**_given(ttl_seconds=timeout))),
                      sandbox_id=sandbox_id, asked={"timeout_seconds": timeout})
        elif timeout is not None:
            self.call(lambda: self.api().update(sandbox_id, UpdateSandboxRequest(ttl_seconds=timeout)),
                      sandbox_id=sandbox_id, asked={"timeout_seconds": timeout})
        self._ready(sandbox_id)
        return Handle(self, sandbox_id)

    def _settle(self, sandbox_id: str, seconds: float = 900):
        """Wait while Boat saves a stopping sandbox's disk."""
        deadline = time.monotonic() + seconds
        while True:
            found = self._sandbox(sandbox_id)
            if found.state != "archiving":
                return found
            if time.monotonic() > deadline:
                raise Busy(f"Boat is still saving sandbox {sandbox_id}'s disk")
            time.sleep(POLL_SECONDS)

    def sandboxes(self, metadata=None, states=None):
        wanted = None
        if states is not None:
            wanted = ",".join(s for state in states for s in (UP if state == RUNNING else STOPPED if state == PAUSED
                                                              else ("error",)))
        labels = [f"{k}={v}" for k, v in (metadata or {}).items()]
        found, cursor = [], None
        while True:
            page = self.call(lambda: self.api().sandboxes(limit=200, cursor=cursor, state=wanted, label=labels or None,
                                                          **self._org_args()))
            found += [self._info(s) for s in page.sandboxes if s.state not in GONE
                      and all((s.labels or {}).get(k) == v for k, v in (metadata or {}).items())]
            info = page.page_info
            if not info or not info.has_more or not info.next_cursor:
                return found
            cursor = info.next_cursor

    def info(self, sandbox_id):
        return self._info(self._sandbox(sandbox_id))

    def kill(self, sandbox_id):
        """Delete it and its snapshots (Boat finishes on its own: async)."""
        self._forget(sandbox_id, gone=True)
        try:
            self.call(lambda: self.api().delete_sandbox(x_ascii_confirm_delete=sandbox_id, sandbox_id=sandbox_id),
                      sandbox_id=sandbox_id)
        except NotRunning:
            return False
        return True

    def pause(self, sandbox_id):
        """Boat's stop: the disk is kept, the machine goes (cold boot on resume)."""
        found = self._sandbox(sandbox_id)
        if found.state == "archived":
            return
        self._forget(sandbox_id)
        if found.state != "archiving":
            try:
                self.call(lambda: self.api().stop(sandbox_id), sandbox_id=sandbox_id, retry_seconds=0)
            except Failed as error:
                if "sandbox_stopping" not in str(error):
                    if isinstance(error, NotRunning):
                        raise
                    raise Busy(f"Boat would not stop sandbox {sandbox_id} now: {error}") from error
        found = self._settle(sandbox_id)
        if found.state != "archived":
            raise Busy(f"Boat did not stop sandbox {sandbox_id}: it is {found.state}")

    def set_timeout(self, sandbox_id, seconds):
        from boat_sdk.models import UpdateSandboxRequest
        found = self.call(lambda: self.api().update(sandbox_id, UpdateSandboxRequest(ttl_seconds=seconds)),
                          sandbox_id=sandbox_id, asked={"timeout_seconds": seconds}).sandbox
        if found.state not in UP:
            raise NotRunning(f"Boat sandbox {sandbox_id} is {found.state}")

    # Snapshots and templates: Boat's saved snapshots

    def _save(self, sandbox_id: str, name: str, seconds: float = 3600) -> str:
        from boat_sdk.models import SavedSnapshotSaveRequest
        name = saved_name(name)
        self.call(lambda: self.api().save_saved_snapshot(SavedSnapshotSaveRequest(sandbox_id=sandbox_id, name=name)),
                  sandbox_id=sandbox_id)
        deadline = time.monotonic() + seconds
        while True:
            found = self.call(lambda: self.api().get_saved_snapshot(name)).snapshot
            if found.status == "ready":
                return name
            if found.status == "failed":
                raise Failed(f"Boat could not save {name}: {found.error or 'failed'}")
            if time.monotonic() > deadline:
                raise Timeout(f"Boat was still saving {name} after {int(seconds)} s")
            time.sleep(POLL_SECONDS)

    def snapshot(self, sandbox_id, name):
        self._refuse_on_trial()
        return Snapshot(self._save(sandbox_id, name))

    def plan(self, fresh: bool = False) -> str:
        """Boat's plan key for the account ("trial" before the first payment), a few minutes old at most."""
        when, found = self._plan
        if fresh or not found or time.monotonic() - when > PLAN_CACHE_SECONDS:
            found = self.call(lambda: self.api().limits(**self._org_args())).sandbox_plan_key or ""
            self._plan = (time.monotonic(), found)
        return found

    def _refuse_on_trial(self):
        """A save is refused on the trial: say so before a builder is started for nothing."""
        if self.plan() == "trial":
            raise LimitRefused(TRIAL_REFUSAL, field="plan")

    def delete_snapshot(self, snapshot_id):
        self.delete_template(snapshot_id)

    def templates(self):
        found = self.call(lambda: self.api().list_saved_snapshots())
        return [(s.name, s.status) for s in found.snapshots]

    def delete_template(self, name):
        try:
            self.call(lambda: self.api().delete_saved_snapshot(saved_name(name)))
        except Failed as error:
            if isinstance(error.__cause__, Exception) and status(error.__cause__) == 404:
                return False
            raise
        return True

    def build_template(self, spec: TemplateSpec, *, alias, cpu, memory_mb, disk_mb, on_log=None):
        """Replay spec in a builder sandbox, save it as alias, delete the
        builder. Refused on the trial before a builder starts; builders this
        installation left behind (older than BUILD_SECONDS) are deleted first."""
        from boat_sdk.models import CreateSandboxRequest
        log = on_log or (lambda line: print(line, flush=True))
        self._refuse_on_trial()
        self._reclaim_builders(log)
        steps = []
        for what, value in spec.steps:
            steps += dockerfile_steps(value) if what == "dockerfile" else [(what, value)]
        parent = next((value for what, value in steps if what == "template"), None)
        image = next((value for what, value in steps if what == "image"), None)
        if image is not None and not re.fullmatch(r"(docker\.io/(library/)?)?ubuntu:24\.04", image):
            raise Failed(f"Boat starts every sandbox from its own Ubuntu 24.04; a template from {image} can't be built "
                         "there. Start it from ubuntu:24.04 or from another template.")
        kind = machine(cpu, memory_mb, disk_mb)
        if kind not in [m[0] for m in MACHINES if m[1] <= (self._limits.cpu or 16)]:
            raise LimitRefused(f"a {cpu} vCPU template needs Boat's {kind} machines, beyond {LIMITS['cpu'][0]}",
                               field="cpu", asked=cpu, allowed=self._limits.cpu)
        request = CreateSandboxRequest(type=kind, ttl_seconds=min(BUILD_SECONDS, self._limits.timeout_seconds or BUILD_SECONDS),
                                       no_env=True, environment=None, name=f"build {alias}"[:120],
                                       labels={INSTALL_LABEL: _installation(), BUILD_LABEL: alias},
                                       **_given(var_from=saved_name(parent) if parent else None), **self._org_args())
        log(f"Boat: starting a {kind} builder" + (f" from {parent}" if parent else " from Boat's Ubuntu 24.04"))
        once = str(uuid.uuid4())   # one key for every retry
        try:
            created = self.call(lambda: self.api().create(idempotency_key=once, create_sandbox_request=request),
                                asked={"cpu": cpu, "memory_mb": memory_mb, "disk_mb": disk_mb})
        except Failed as error:
            if parent and _gone_snapshot(error):
                raise TemplateGone(parent, f"Boat has no saved snapshot {saved_name(parent)}") from error
            raise
        builder = created.sandbox.id
        try:
            self._ready(builder)
            self._replay(Handle(self, builder), steps, log)
            log(f"Boat: saving {alias}")
            self._save(builder, alias)
            log(f"Boat: {alias} is ready")
            return alias
        finally:
            try:
                self.kill(builder)
            except Exception as error:   # its auto-stop ends it; Boat keeps its disk 14 days
                log(f"Boat: could not delete builder {builder}: {error}")

    def _reclaim_builders(self, log):
        """Delete this installation's builders older than BUILD_SECONDS: a
        build that died with sbx-control left them (labelled sbx_build)."""
        try:
            found = self.call(lambda: self.api().sandboxes(limit=200, label=[f"{INSTALL_LABEL}={_installation()}"],
                                                          **self._org_args())).sandboxes
        except Exception as error:   # the build goes on; the next one looks again
            log(f"Boat: could not list old builders: {error}")
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        for sandbox in found:
            if BUILD_LABEL in (sandbox.labels or {}) and sandbox.created_at and \
                    (now - sandbox.created_at).total_seconds() > BUILD_SECONDS:
                log(f"Boat: deleting builder {sandbox.id}, left from an earlier build")
                try:
                    self.kill(sandbox.id)
                except Exception as error:
                    log(f"Boat: could not delete builder {sandbox.id}: {error}")

    def env_file(self, sandbox_id: str, envs: dict) -> str:
        """envs as a file in the sandbox (ENV_DIR, 0700), through the files
        API: what a command reads once and removes (wrap)."""
        if sandbox_id not in self._env_dirs:
            Handle(self, sandbox_id).commands.run(f"install -d -m 700 {ENV_DIR}", timeout=30)
            self._env_dirs.add(sandbox_id)
        path = f"{ENV_DIR}/{uuid.uuid4().hex}"
        Handle(self, sandbox_id).files._put(path, "".join(export_line(k, v) for k, v in envs.items()).encode())
        return path

    def _replay(self, handle: Handle, steps: list, log):
        user, workdir, env = "user", HOME, {}
        commands = handle.commands
        for what, value in steps:
            if what in ("image", "template"):
                continue
            if what == "user":
                user = value
            elif what == "workdir":
                workdir = value
                commands.run(f"sudo mkdir -p {shlex.quote(value)} && sudo chown {shlex.quote(user)} "
                             f"{shlex.quote(value)} 2>/dev/null || true", timeout=60)
            elif what == "env":
                env.update(value)
            elif what == "run":
                log(f"RUN {value[:200]}")
                commands.run(value, user=user, envs=env, cwd=workdir, timeout=0, on_output=lambda text: [
                    log(line) for line in text.splitlines() if line.strip()])
            elif what == "start":
                self._persist(commands, env, workdir)
                command, ready = value
                self._start_unit(commands, command, ready, log)
                continue
            else:
                raise ValueError(f"unknown template step {what}")
            self._persist(commands, env, workdir)

    @staticmethod
    def _persist(commands: _Commands, env: dict, workdir: str):
        """The template's env and workdir as every later command and shell sees them."""
        text = "".join(export_line(k, v) for k, v in env.items())
        text += f"export SBX_TEMPLATE_WORKDIR={shlex.quote(workdir)}\n"
        encoded = base64.b64encode(text.encode()).decode()
        commands.run(f"sudo mkdir -p /etc/sbx && echo {encoded} | base64 -d | sudo tee {TEMPLATE_FILE} >/dev/null "
                     f"&& sudo chmod 644 {TEMPLATE_FILE} && echo '[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}' | "
                     "sudo tee /etc/profile.d/sbx-template.sh >/dev/null", timeout=60)

    @staticmethod
    def _start_unit(commands: _Commands, command: str, ready, log):
        """The start command as an enabled systemd unit (Boat starts enabled
        units again on every resume), started now and waited for."""
        script = f"#!/bin/bash\n[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}\ncd \"${{SBX_TEMPLATE_WORKDIR:-$HOME}}\"\n" \
                 f"exec {command}\n"
        unit = ("[Unit]\nDescription=sbx start command\nAfter=network.target\n[Service]\nUser=user\n"
                "ExecStart=/bin/bash /etc/sbx/start.sh\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n")
        for path, text in (("/etc/sbx/start.sh", script), (f"/etc/systemd/system/{START_UNIT}.service", unit)):
            encoded = base64.b64encode(text.encode()).decode()
            commands.run(f"echo {encoded} | base64 -d | sudo tee {path} >/dev/null", timeout=60)
        commands.run(f"sudo systemctl daemon-reload && sudo systemctl enable --now {START_UNIT}", timeout=120)
        check = f"ss -Hltn 'sport = :{ready}' | grep -q ." if isinstance(ready, int) else ready
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                commands.run(check, timeout=30)
                log("Boat: the start command is ready")
                return
            except (CommandFailed, Timeout):
                time.sleep(POLL_SECONDS)
        raise Failed(f"the template's start command was not ready after 300 s ({check})")

    def failure(self, error):
        return type(error).__module__.split(".")[0] in ("boat_sdk", "urllib3")

    # What the settings page and the first-run setup show

    def why_busy(self, memory_mb):
        return "Boat would not stop the sandbox now (it may be saving its disk); pause again in a minute"

    def readiness(self, facts):
        from ... import vault
        from ...connections.base import Check
        return [Check("Boat configured", bool(vault.runtime((API_KEY,)).get(API_KEY)), "a Boat API key",
                      f"Add it: sbx-admin runtime add {KIND} (the key on stdin).")]

    def _account(self) -> dict:
        """What Boat says of the account: its plan, and the key's expiry when Boat lists it."""
        found = self.call(lambda: self.api().limits(**self._org_args()))
        self._plan = (time.monotonic(), found.sandbox_plan_key or "")
        facts = {"plan": found.sandbox_plan_key or "", "running_at_boat": found.active_sandboxes,
                 "at_once": found.max_active_sandboxes}
        try:
            raw = self.api().api_keys_without_preload_content()
            keys = json.loads(raw.data or b"{}").get("apiKeys") or []
            mine = next((k for k in keys if self._key.endswith(k.get("keyLastFour") or "\0")), None)
            if mine and mine.get("expiresAt"):
                facts["key_expires_at"] = mine["expiresAt"]
        except Exception:   # a scoped key may not list keys: no expiry warning then
            pass
        return facts

    def _expiry_warning(self, facts: dict) -> str:
        when = facts.get("key_expires_at")
        if not when:
            return ""
        try:
            at = datetime.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        except ValueError:
            return ""
        days = (at - datetime.datetime.now(datetime.timezone.utc)).days
        if days >= KEY_WARN_DAYS:
            return ""
        return "the API key has expired" if days < 0 else f"the API key expires in {days} days"

    def status(self):
        from ...connections.base import Field, Status
        if not self._key:
            return Status("runtime", "Sandbox runtime", "not_connected", f"Boat: no {API_KEY} in runtime.env",
                          {}, (), ("test",))
        try:
            from ... import runtime   # this installation's sandboxes, not the whole account's
            running = len(runtime.sandboxes(None, [runtime.RUNNING], kind=self.kind))
            facts = self._account()
        except Exception as error:
            return Status("runtime", "Sandbox runtime", "attention", f"Boat did not answer: {error}", {}, (),
                          ("test",))
        facts["running"] = running
        notes = []
        if facts.get("plan") == "trial":
            notes.append(TRIAL_NOTE.rstrip("."))
        if warning := self._expiry_warning(facts):
            notes.append(warning)
        most = self._limits.sandboxes
        full = most is not None and running >= most
        summary = " · ".join([f"Boat · {running} running here", *notes, *(["full"] if full else [])])
        return Status("runtime", "Sandbox runtime", "attention" if notes or full else "connected", summary, facts,
                      (Field("api_url", "API URL", value=self._url),), ("test",))

    def test(self):
        from ...connections.base import Check
        try:
            facts = self._account()
            count = len(self.templates())
        except Exception as error:
            return [Check("Boat API", False, f"{type(error).__name__}: {error}",
                          f"check {API_KEY} in /etc/sbx/runtime.env (sbx-admin runtime add {KIND})")]
        checks = [Check("Boat API", True, f"answers; plan {facts.get('plan') or 'unknown'}; {count} saved snapshots")]
        if facts.get("plan") == "trial":
            checks.append(Check("Boat plan", False, "the trial: 2 sandboxes at once, up to 2 hours, no saved snapshots",
                                "Templates need the first payment: Boat answers \"Saved snapshots are not available "
                                "during the free trial\". Pay in Boat's dashboard (Billing), then build again."))
        if warning := self._expiry_warning(facts):
            checks.append(Check("Boat API key", False, warning, "create a new key and add it again"))
        return checks
