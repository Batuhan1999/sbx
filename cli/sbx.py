#!/usr/bin/env python3
"""sbx: dev sandboxes on the sbx-control that installed it.

A thin client of its HTTP API: everything it shows or connects to (URLs,
the SSH bridge, readiness, where agent logins go, recipe scaffolds) comes
from the server. `sbx COMMAND -h` explains a command.
"""
import argparse
import base64
import calendar
import contextlib
import fcntl
import getpass
import hashlib
import http.client
import http.server
import io
import json
import os
import pathlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

INSTALL_ROOT = pathlib.Path.home() / ".local/share/sbx"
# The installer records where it came from; the release carries its server's.
_control_files = [INSTALL_ROOT / "control", pathlib.Path(__file__).resolve().parent / "control"]
CONTROL = os.environ.get("SBX_CONTROL") or next((f.read_text().strip() for f in _control_files if f.exists()), "")
HOME = pathlib.Path.home()
SSH_DIR = HOME / ".ssh" / "sbx"
SSH_CONFIG = pathlib.Path.home() / ".ssh" / "config"
SSH_BLOCK = ("# >>> sbx sandboxes (managed by sbx) >>>", "# <<< sbx sandboxes <<<")
CLAUDE_DESKTOP_SSH = pathlib.Path.home() / "Library/Application Support/Claude/ssh_configs.json"
SBX_BIN = str(pathlib.Path.home() / ".local/bin/sbx")
API_TOKEN_SERVICE = "sbx-api-token"
_version_file = pathlib.Path(__file__).resolve().parent / "VERSION"
VERSION = _version_file.read_text().strip() if _version_file.exists() else "dev"


def keychain_get(service):
    found = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 else None


def keychain_set(service, value):
    subprocess.run(["security", "add-generic-password", "-U", "-a", os.environ.get("USER", "sbx"), "-s", service,
                    "-w", value], check=True)


class Refused(Exception):
    """sbx-control answered with an error (status: its HTTP status), or there is no session."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def tls_or_loopback(url):
    """https, or http only to this machine itself (a tunnel)."""
    url = urllib.parse.urlsplit(url)
    return url.scheme == "https" or (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1"))


class Client:
    """sbx-control at base, signed in with the session's API token from the
    Keychain, or with a first-run setup link's token (Authorization: Setup)
    over that link's own TLS context (its certificate may be the self-signed
    one the installer made: --insecure)."""

    def __init__(self, base, setup_token=None, context=None):
        self.base, self.setup_token, self.context = base, setup_token, context

    def _authorization(self):
        if not tls_or_loopback(self.base):   # a token never crosses a network in plain text
            raise Refused(f"{self.base} is neither https nor this machine: sbx sends no token to it" if self.base
                          else "no server: set SBX_CONTROL to its https address")
        if self.setup_token:
            return f"Setup {self.setup_token}"
        token = keychain_get(API_TOKEN_SERVICE)
        if not token:
            raise Refused("not signed in: run `sbx login`")
        return f"Bearer {token}"

    def request(self, method, path, body=None, raw=False, timeout=600):
        call = urllib.request.Request(f"{self.base}{path}", method=method,
                                      data=json.dumps(body).encode() if body is not None else None,
                                      headers={"Authorization": self._authorization(),
                                               "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(call, timeout=timeout, context=self.context) as response:
                server = response.headers.get("X-Sbx-Version")
                if server and VERSION not in ("dev", server):
                    print(f"sbx {server} is available (you have {VERSION}): sbx update", file=sys.stderr)
                data = response.read()
                return data.decode() if raw else json.loads(data or b"null")
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            if isinstance(detail, dict):   # {message, checks}: what failed, each check that did, and how to fix it
                message = detail.get("message") or "refused"
                detail = "; ".join([message, *(check_line(c) for c in detail.get("checks") or []
                                               if not c.get("ok") and c.get("detail") != message)])
            if error.code == 401:
                raise Refused("the setup link was refused: it is used up or replaced (sbx-admin setup-link prints the "
                              "current one)" if self.setup_token else "session expired: run `sbx login`", 401) from None
            raise Refused(f"sbx-control: {detail}", error.code) from None

    def api(self, method, path, body=None, raw=False, timeout=600, late=None):
        """request(), exiting with the refusal or why the server could not be reached (request() itself
        raises OSError, which follow() retries while sbx-control restarts); with late, when the request
        was sent but no answer came (a timeout or a dropped connection): never sent again."""
        try:
            return self.request(method, path, body, raw, timeout)
        except Refused as error:
            sys.exit(str(error))
        except (OSError, http.client.HTTPException) as error:
            # urlopen wraps a failure to connect or send in URLError; one while waiting for the reply is raw.
            if late and not isinstance(error, urllib.error.URLError):
                sys.exit(late)
            reason = getattr(error, "reason", None) or error
            hint = "; add --insecure for a self-signed certificate" if self.setup_token and self.context is None else ""
            sys.exit(f"cannot reach {self.base}: {reason}{hint}")


def request(method, path, body=None, raw=False, timeout=600):
    return Client(CONTROL).request(method, path, body, raw, timeout)


def api(method, path, body=None, raw=False, timeout=600, late=None):
    return Client(CONTROL).api(method, path, body, raw, timeout, late)


def note(text):
    print(text, file=sys.stderr)


RETRIED = (502, 503, 504)   # sbx-control restarting behind nginx
HEARTBEAT_SECONDS = 25   # how often at least a wait says it is still waiting (well within 30 s)


def seconds(text):
    """A --timeout: a whole number of seconds, at least 1."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text}: a whole number of seconds, at least 1")
    return value


def waited(elapsed, timeout):
    return f"{int(elapsed)} s of up to {int(timeout)} s"


def gave_up(message, timeout):
    """A wait's exit once its --timeout ran out: what is still pending, then the limit that ended it."""
    return f"{message} (gave up after {int(timeout)} s; --timeout sets how long to wait)"


@contextlib.contextmanager
def ticking(what, timeout):
    """While one long call waits for its answer: '<what> (N s of up to T s)' on stderr every
    HEARTBEAT_SECONDS, from a thread that stops with the call."""
    import threading
    stop, started = threading.Event(), time.monotonic()

    def tick():
        while not stop.wait(HEARTBEAT_SECONDS):
            note(f"{what} ({waited(time.monotonic() - started, timeout)})")
    threading.Thread(target=tick, daemon=True).start()
    try:
        yield
    finally:
        stop.set()


def follow(fetch, verdict, timeout, *, interval=2.0, first=None, retry_statuses=(), late="still waiting"):
    """What fetch() answers, followed until verdict(found) -> (progress, done)
    says it is done: then that answer. Progress (text or None) is noted when
    it changes, and again with the time waited whenever HEARTBEAT_SECONDS pass
    without a note; done is True, False, or why it failed (a text: it exits with
    it). first: an answer in hand already. A fetch refused with one of retry_statuses, or a dropped
    connection while there are any, is tried again; any other refusal exits.
    Past timeout: exits non-zero with late (or late(found)) and the limit."""
    started = time.monotonic()
    deadline, said, noted = started + timeout, None, started
    found, fetching = first, first is None
    while True:
        if fetching:   # the first fetch too is tried again as the later ones are
            try:
                found = fetch()
            except Refused as error:
                if error.status not in retry_statuses:
                    sys.exit(str(error))
            except OSError:   # refused or dropped connections while it restarts
                if not retry_statuses:
                    raise
        fetching = True
        if found is not None:
            progress, done = verdict(found)
            if progress and progress != said:
                note(progress)
                said, noted = progress, time.monotonic()
            if isinstance(done, str):
                sys.exit(done)
            if done:
                return found
        now = time.monotonic()
        if now >= deadline:
            sys.exit(gave_up(late(found) if callable(late) and found is not None else late if isinstance(late, str)
                             else "still waiting", timeout))
        if now - noted >= HEARTBEAT_SECONDS:
            note(f"{(said or 'still waiting').strip()} ({waited(now - started, timeout)})")
            noted = now
        if interval:
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def login_control(timeout=300):
    """Browser sign-in: sbx-control hands a loopback listener a one-time
    code, which is exchanged here, with the verifier only this process
    knows, for the API token. Waits up to timeout seconds for the browser."""
    state, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    received = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] == state and query.get("code"):
                received["code"] = query["code"][0]
                text = "Signed in. You can close this tab."
            else:
                text = "Sign-in failed: state mismatch."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(text.encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Callback)
    query = {"port": server.server_address[1], "state": state, "challenge": challenge}
    url = f"{CONTROL}/cli/login?{urllib.parse.urlencode(query)}"
    print(f"Opening {url}")
    webbrowser.open(url)
    started = time.monotonic()
    deadline, noted = started + timeout, started
    while "code" not in received and time.monotonic() < deadline:
        server.timeout = 1
        server.handle_request()
        if time.monotonic() - noted >= HEARTBEAT_SECONDS and "code" not in received:
            noted = time.monotonic()
            note(f"waiting for the sign-in in your browser ({waited(noted - started, timeout)})")
    server.server_close()
    if "code" not in received:
        sys.exit(gave_up("the browser sign-in did not finish", timeout))
    exchange = urllib.request.Request(f"{CONTROL}/cli/token", method="POST",
                                      data=json.dumps({"code": received["code"], "verifier": verifier}).encode(),
                                      headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(exchange, timeout=30) as response:
            token = json.loads(response.read())["token"]
    except urllib.error.HTTPError as error:
        sys.exit(f"sign-in failed: HTTP {error.code}")
    keychain_set(API_TOKEN_SERVICE, token)
    print(f"signed in as {api('GET', '/api/me')['email']}")


# An agent's login is the developer's own (the server's /api/me says, per
# agent, how to get it and where it goes in a sandbox): a token kept in the
# Keychain, or the file the agent's own login writes.

def agents():
    return api("GET", "/api/me").get("agents") or {}


def token_service(kind):
    return f"sbx-{kind}-oauth-token"


def agent_login(kind, agent):
    """The developer's login for the agent, or None: not on this Mac."""
    if agent.get("env"):
        return keychain_get(token_service(kind))
    path = HOME / agent["file"].removeprefix("~/")
    return path.read_text() if path.exists() else None


def login(args):
    if args.agent is None:
        login_control(getattr(args, "timeout", 300))
        return
    found = agents()
    agent = found.get(args.agent) or sys.exit(f"no agent {args.agent} on this server: {', '.join(found) or 'none'}")
    if agent.get("env"):
        print(f"{agent['title']} signs you in; copy the token it prints.")
        subprocess.run(agent["get"], check=True)
        keychain_set(token_service(args.agent), getpass.getpass("paste the token (hidden): ").strip())
        print("stored in your Keychain")
        return
    if agent_login(args.agent, agent) is None:
        subprocess.run(agent["get"], check=True)
    print(f"{agent['command']} will use {agent['file']}")


# SSH. `sbx up` writes ~/.ssh/sbx/NAME.conf and the host key the server
# returned; sbx's own ssh calls pass those options on the command line, and
# `sbx ssh-config` makes them visible to everything else (ssh, editors, apps).

def write_host(name, info):
    if not info.get("ssh_url"):
        note(f"sbx-control names no SSH bridge for {name}: ssh, open, logs and push won't reach it")
        return False
    SSH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    known = SSH_DIR / "known_hosts"
    (SSH_DIR / f"{name}.conf").write_text(
        f"Host sbx-{name}\n"
        f"  HostName {info['sandbox_id']}\n"
        f"  User user\n"
        f"  HostKeyAlias sbx-{name}\n"
        f"  UserKnownHostsFile \"{known}\"\n"
        f"  StrictHostKeyChecking yes\n"
        # Absolute path: GUI apps (Claude Desktop, the Codex app) run ssh
        # without ~/.local/bin on PATH.
        f"  ProxyCommand {shlex.quote(SBX_BIN)} tunnel %h {info['ssh_url']}\n"
        f"  ServerAliveInterval 30\n")
    lines = [line for line in (known.read_text().splitlines() if known.exists() else [])
             if not line.startswith(f"sbx-{name} ")]
    known.write_text("\n".join(lines + [f"sbx-{name} {info['host_key']}"]) + "\n")
    return True


def forget_host(name):
    (SSH_DIR / f"{name}.conf").unlink(missing_ok=True)


def ssh_target(name):
    conf = SSH_DIR / f"{name}.conf"
    if not conf.exists():
        sys.exit(f"this Mac has no SSH config for sbx-{name}; `sbx up {name}` writes it")
    options = []
    for line in conf.read_text().splitlines()[1:]:
        key, _, value = line.strip().partition(" ")
        if key:
            options += ["-o", f"{key}={value}"]
    return options + [f"sbx-{name}"]


def ssh_config_installed():
    return SSH_CONFIG.exists() and SSH_BLOCK[0] in SSH_CONFIG.read_text()


@contextlib.contextmanager
def ssh_config_lock():
    """One read-edit-write of ~/.ssh/config at a time (`sbx open`, `sbx
    ssh-config`), so two at once don't undo each other's change."""
    SSH_CONFIG.parent.mkdir(mode=0o700, exist_ok=True)
    with open(SSH_CONFIG.parent / ".sbx-config.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # released when the file closes
        yield


def write_atomic(path, text, mode=None):
    """Replace the file in one step, so an ssh reading it never sees half of
    it; through a symlink to its target, keeping its mode (or the given one)."""
    target = pathlib.Path(os.path.realpath(path))
    if mode is None:
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".sbx-tmp")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def rewrite_ssh_config(block):
    """block: the text of sbx's block, or a function making it, called
    under the lock (so what it reads is what gets published)."""
    with ssh_config_lock():
        text = SSH_CONFIG.read_text() if SSH_CONFIG.exists() else ""
        begin, end = SSH_BLOCK
        if begin in text and end in text:
            text = text[:text.index(begin)] + text[text.index(end) + len(end):].lstrip("\n")
        write_atomic(SSH_CONFIG, (block() if callable(block) else block) + text, 0o600)


def claude_desktop_hosts(names):
    """Claude Desktop's saved SSH connections: exactly these sandboxes among
    the sbx-* ones, so they appear in the Code tab's environment picker.
    False when Claude Desktop is not installed."""
    if not CLAUDE_DESKTOP_SSH.parent.exists():
        return False
    data = json.loads(CLAUDE_DESKTOP_SSH.read_text()) if CLAUDE_DESKTOP_SSH.exists() else {}
    configs = [c for c in data.get("configs", []) if not str(c.get("sshHost", "")).startswith("sbx-")]
    trusted = [h for h in data.get("trustedHosts", []) if not str(h).startswith("sbx-")]
    for name in names:
        configs.append({"name": f"sandbox {name}", "sshHost": f"sbx-{name}", "id": str(uuid.uuid4())})
        trusted.append(f"sbx-{name}")
    data.update(configs=configs, trustedHosts=trusted)
    tmp = CLAUDE_DESKTOP_SSH.with_suffix(".sbx-tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(CLAUDE_DESKTOP_SSH)
    return True


def ssh_config_cmd(args):
    if args.remove:
        rewrite_ssh_config("")
        print(f"removed sbx's block from {SSH_CONFIG}" + (" and its hosts from Claude Desktop"
                                                         if claude_desktop_hosts([]) else ""))
        return
    confs = sorted(SSH_DIR.glob("*.conf"))
    begin, end = SSH_BLOCK

    def block():
        lines = [begin, f"Include \"{SSH_DIR}/*.conf\""]
        if args.apps:
            # Claude Desktop and the Codex app list hosts from this file and may
            # not follow Include: copies, current until the next up or down.
            lines += [conf.read_text().rstrip("\n") for conf in confs]
        return "\n".join(lines) + f"\n{end}\n\n"
    rewrite_ssh_config(block)
    print(f"{SSH_CONFIG} includes {SSH_DIR}: ssh, VS Code, Cursor and Zed reach sbx-NAME")
    if args.apps:
        names = [conf.stem for conf in confs]
        registered = claude_desktop_hosts(names)
        print(f"copied {len(names)} host(s) for GUI apps{' and registered them in Claude Desktop' if registered else ''}; "
              "run `sbx ssh-config --apps` again after up or down")


def websocat():
    return shutil.which("websocat", path=f"/opt/homebrew/bin:/usr/local/bin:{os.environ.get('PATH', '')}")


SSH_REACH_SECONDS = 120   # how long the tunnel waits for a sandbox being made or waking up


def tunnel(args):
    """SSH ProxyCommand: the sandbox's sshd over a WebSocket through the
    gate in front of the sandboxes, which checks this token and that you own
    the sandbox."""
    token = keychain_get(API_TOKEN_SERVICE)
    if not token:
        sys.exit("not signed in: run `sbx login`")
    if urllib.parse.urlsplit(args.url).scheme != "wss":   # the token goes only over TLS
        sys.exit(f"{args.sandbox_id}: the SSH bridge must be a wss:// URL, not {args.url}")
    binary = websocat() or sys.exit("websocat not found: brew install websocat")
    bridge_ready(args.sandbox_id, args.url, token)
    os.execv(binary, [binary, "--binary", "-B", "65536", f"--header=Authorization: Bearer {token}", "-", args.url])


def bridge_status(url, token, timeout=30):
    """The status the gate answers a WebSocket upgrade to the bridge with
    (101 once it is let through), its Retry-After and the page sbx answered
    with (X-Sbx-Page; None for any other answer)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "wss":
        raise ValueError(f"not a wss:// URL: {url}")
    connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
    try:
        connection.request("GET", parts.path or "/", headers={
            "Authorization": f"Bearer {token}", "Connection": "Upgrade", "Upgrade": "websocket",
            "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": base64.b64encode(secrets.token_bytes(16)).decode()})
        response = connection.getresponse()
        return response.status, response.getheader("Retry-After"), response.getheader("X-Sbx-Page")
    finally:
        connection.close()


def bridge_ready(host, url, token):
    """Before ssh's bytes go over the bridge: while the gate says the
    sandbox is on its way (503: being made, waking up), or something past
    it answers a 502 of its own (the bridge not up yet), wait, saying so on
    stderr (ssh shows it); a bridge that names no sandbox (404), refuses
    (a sign-in, 403) or whose VM failed (sbx's failed page, a 502) exits with
    what to do. Anything else is websocat's to meet. host: the sandbox, as
    ssh's %h names it."""
    deadline, said, noted = time.monotonic() + SSH_REACH_SECONDS, False, SSH_REACH_SECONDS
    late = f"{host}: still not ready after {SSH_REACH_SECONDS}s; `sbx sandbox info NAME --wait` follows it"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:   # no probe after the budget, and none outlasting it
            sys.exit(late)
        if said and noted - remaining >= HEARTBEAT_SECONDS:   # the clock read above: no other
            noted = remaining
            note(f"{host}: still waiting for the sandbox ({waited(SSH_REACH_SECONDS - remaining, SSH_REACH_SECONDS)})")
        try:
            status, retry, page = bridge_status(url, token, timeout=remaining)
        except (OSError, http.client.HTTPException) as error:
            sys.exit(f"{host}: the SSH bridge at {url} did not answer: {error}")
        if status == 404:
            sys.exit(f"{host}: sbx-control finds no sandbox of yours at {url}. If its address changed, "
                     f"`sbx open NAME ssh` points its SSH config at it again (`sbx up NAME` if it is gone). If ssh "
                     f"then reports a changed host key, it was recreated: only the `sbx up` that made it received "
                     f"its key, so use that Mac")
        if status in (302, 401, 403):   # 302: nginx sends a request it can't sign in to the login
            sys.exit(f"{host}: sbx-control refused the SSH bridge ({status}): `sbx login` again, or it is "
                     f"not your sandbox")
        if status == 502 and page:   # sbx's page (failed, the app stopped): the runtime reports an error
            sys.exit(f"{host}: the sandbox's runtime reports an error; `sbx ls` shows it, restart or delete it in sbx")
        if status not in (502, 503):
            return
        if time.monotonic() >= deadline:
            sys.exit(late)
        if not said:
            note(f"{host}: waiting for the sandbox to be ready ...")
            said, noted = True, remaining
        retry = (retry or "").strip()
        # Delay-seconds only: an HTTP date, a negative or any other value waits 2 s.
        # Leading zeros dropped and capped by length first: int() of thousands of digits raises.
        digits = retry.lstrip("0") or "0"
        wait = (float(min(int(digits), 10)) if len(digits) <= 2 else 10.0) \
            if retry.isascii() and retry.isdigit() else 2.0
        time.sleep(min(wait, max(deadline - time.monotonic(), 0)))
        if time.monotonic() >= deadline:   # no probe after the budget
            sys.exit(late)


def hand_credentials(name):
    """Your agents' logins go from this Mac straight into the sandbox's
    memory (tmpfs), where the server says each belongs; sbx-control never
    sees them."""
    drops = []
    for kind, agent in agents().items():
        value = agent_login(kind, agent)
        if value is not None:
            drops.append((agent["path"], f"export {agent['env']}={shlex.quote(value)}\n" if agent.get("env") else value))
    for path, data in drops:
        done = subprocess.run(["ssh", "-o", "BatchMode=yes", *ssh_target(name),
                               f"mkdir -p {shlex.quote(os.path.dirname(path))} && umask 077 && cat > {shlex.quote(path)}"],
                              input=data, text=True, capture_output=True)
        if done.returncode:
            note(f"could not hand your agent login to sbx-{name}: {done.stderr.strip()}")
            return


def public_keys():
    keys = [p.read_text().strip() for p in (HOME / ".ssh").glob("id_*.pub")]
    return "\n".join(keys) + "\n" if keys else ""


def find_repo(explicit=None):
    if explicit:
        return os.path.expanduser(explicit)
    here = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return here.stdout.strip() if here.returncode == 0 else None


def github_repo(checkout):
    url = subprocess.run(["git", "-C", checkout, "remote", "get-url", "origin"],
                         capture_output=True, text=True).stdout.strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            return url[len(prefix):].removesuffix("/").removesuffix(".git")
    return None


def current_branch(checkout):
    return subprocess.run(["git", "-C", checkout, "rev-parse", "--abbrev-ref", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def pushed_branch(checkout):
    """The checkout's branch if GitHub has it, else None: the server uses
    the repo's default branch."""
    branch = current_branch(checkout)
    if subprocess.run(["git", "-C", checkout, "ls-remote", "--exit-code", "--heads", "origin", branch],
                      capture_output=True).returncode != 0:
        note(f"{branch} is not on GitHub; using the repo's default branch (push it, or `sbx push NAME` after up)")
        return None
    return branch


def this_repo(explicit=None):
    if explicit:
        return explicit.lower()
    checkout = find_repo()
    repo = github_repo(checkout) if checkout else None
    if not repo:
        sys.exit("run this inside a checkout of a GitHub repo, or pass --repo owner/name")
    return repo.lower()


def failed(info):
    """Its last operation failed (a running one may still recover it)."""
    record = info.get("lifecycle") or {}
    return not record.get("active") and (record.get("phase") in ("failed", "interrupted")
                                         or record.get("application") == "failed")


def cause(name):
    """Why the sandbox's last operation failed, as its newest failure event
    says (the command, its exit code, the end of its output); "" when none."""
    try:
        events = request("GET", f"/api/sandboxes/{name}/detail").get("events") or []
    except (Refused, OSError):
        return ""
    return next((e["detail"] for e in events if e["kind"].endswith(" failed") and e["detail"]), "")


def wait_for(name, timeout):
    """Follow the sandbox's operation until it finishes (the server holds
    each request until the phase changes); exit 1 when it failed."""
    def verdict(info):
        record = info.get("lifecycle") or {}
        why = cause(name) if failed(info) else ""
        return f"  {(record.get('phase') or 'unknown').replace('_', ' ')}", (
            f"{name} failed:\n{info.get('error') or '(no error recorded)'}\n{why + chr(10) if why else ''}"
            f"fix it, then `sbx sandbox prepare {name}`; `sbx logs {name}` follows the app"
            if failed(info) else not record.get("active"))
    # The server holds each poll up to 20 s, so the wait notes its progress within 30 s.
    return follow(lambda: api("GET", f"/api/sandboxes/{name}?wait=20", timeout=90), verdict, timeout, interval=0,
                  late=lambda info: f"{name} is still {((info.get('lifecycle') or {}).get('phase') or 'starting').replace('_', ' ')}; "
                                    f"`sbx sandbox info {name} --wait` follows it")


def outcome(info):
    application = (info.get("lifecycle") or {}).get("application")
    if application == "healthy":
        return "the app is up"
    if application == "unverified":
        return "the app started; add health checks to verify it"
    return f"ready: {info.get('readiness', 'unknown').replace('_', ' ')}"


def print_urls(info, name):
    links = "".join(f"  {label:<21} {url}\n" for label, url in info.get("urls", {}).items())
    links += "".join(f"  {'port-' + str(p['port']):<21} listens on localhost only; start it with --host 0.0.0.0\n"
                     for p in info.get("ports", []) if p.get("loopback"))
    print(f"""
  shell / any harness   ssh sbx-{name}          (repo at {info['repo']})
{links}  agent or editor       sbx open {name} AGENT | code | cursor | zed
""")


def template_progress():
    """One line on the templates still building (GET /api/templates), or ""."""
    try:
        found = Client(CONTROL).request("GET", "/api/templates", None, False, 30)
    except (Refused, OSError, http.client.HTTPException):
        return ""
    building = [t for ts in found.get("runtimes", {}).values() for t in ts if t.get("phase") != "current"]
    return "; ".join(f"{t['key']} {t.get('phase')}" + (f" step {t['step']}/{t['steps']}" if t.get("steps") else "")
                     for t in building)


def create_when_built(body, started, timeout):
    """POST /api/sandboxes, waiting while the template it starts from is still building (503 with
    Retry-After, the first time on a new server): progress every heartbeat, until timeout."""
    deadline = started + timeout
    said = 0.0
    while True:
        try:
            return Client(CONTROL).request("POST", "/api/sandboxes", body, False, max(30, deadline - time.time()))
        except Refused as refused:
            if refused.status != 503 or "template" not in str(refused) or time.time() >= deadline:
                sys.exit(str(refused))
            last = refused
        except urllib.error.URLError:
            return api("POST", "/api/sandboxes", body)   # never delivered: api() retries it or says why
        except (OSError, http.client.HTTPException) as error:
            # sent, but no answer: the sandbox may exist, so don't create it again
            sys.exit(f"no answer to creating {body['name']} ({error}); see sbx ls before running sbx up again")
        if time.time() - said >= HEARTBEAT_SECONDS:
            progress = template_progress()
            note(f"[{time.time() - started:4.1f}s] waiting for the templates{': ' + progress if progress else ''}")
            said = time.time()
        time.sleep(min(20, max(1, deadline - time.time())))
        if time.time() >= deadline:
            sys.exit(str(last))


def up(args):
    started = time.time()
    repo, branch = args.repo, args.branch
    if not repo:
        checkout = find_repo()
        repo = github_repo(checkout) if checkout else None
        if repo and not branch:
            branch = pushed_branch(checkout)
    keys = public_keys()
    if not keys:
        note("no ~/.ssh/id_*.pub: SSH into the sandbox won't work (ssh-keygen -t ed25519)")
    body = {"name": args.name, "branch": branch, "public_keys": keys, "repo": repo}
    if args.runtime:
        body["runtime"] = args.runtime
    info = create_when_built(body, started, args.timeout)
    if write_host(args.name, info) and keys:
        hand_credentials(args.name)
    note(f"[{time.time() - started:4.1f}s] sbx-{args.name} is on {info.get('branch') or branch or 'the default branch'}")
    if not args.no_wait:
        info = wait_for(args.name, max(1, started + args.timeout - time.time()))
        note(f"[{time.time() - started:4.1f}s] {outcome(info)}")
    if args.json:
        print(json.dumps({**info, "ssh_host": f"sbx-{args.name}"}))
    else:
        print_urls(info, args.name)
        if not ssh_config_installed():
            print(f"  `ssh sbx-{args.name}` from anywhere needs `sbx ssh-config` once")
    if args.open:
        open_tool(args.name, args.open)


EDITORS = ("code", "cursor", "zed", "ssh")


def refresh_host(name, info):
    """Point this Mac's SSH config for the sandbox at the bridge the server
    names now (its address may have changed since `sbx up` wrote it)."""
    conf = SSH_DIR / f"{name}.conf"
    if not info.get("ssh_url") or not conf.exists():
        return
    def point(text):
        return re.sub(r"^(  ProxyCommand .* tunnel %h ).*$", lambda m: m.group(1) + info["ssh_url"], text, flags=re.M)

    with ssh_config_lock():   # so `sbx ssh-config --apps` copies it before or after, never half
        text = conf.read_text()
        if point(text) != text:
            write_atomic(conf, point(text))
    # The copy `sbx ssh-config --apps` made for GUI apps, if there is one:
    # checked on every open, so one an earlier open could not update is now.
    # Optional: ssh itself reads the file above, so a failure here only says so.
    begin, end = SSH_BLOCK
    try:
        with ssh_config_lock():
            copied = SSH_CONFIG.read_text() if SSH_CONFIG.exists() else ""
            if begin not in copied or end not in copied:
                return
            start, stop = copied.index(begin), copied.index(end)
            host = re.compile(rf"^Host sbx-{re.escape(name)}\n(?:  .*\n)*", re.M).search(copied, start, stop)
            if host and point(host.group(0)) != host.group(0):
                write_atomic(SSH_CONFIG, copied[:host.start()] + point(host.group(0)) + copied[host.end():])
    except OSError as error:
        note(f"the GUI apps' copy of sbx-{name} in {SSH_CONFIG} was not updated ({error}); "
             f"`sbx ssh-config --apps` retries")


def open_tool(name, tool):
    info = api("GET", f"/api/sandboxes/{name}")
    refresh_host(name, info)
    repo = info["repo"]
    commands = {agent["command"] for agent in agents().values()}
    if tool in commands:
        hand_credentials(name)
        os.execvp("ssh", ["ssh", "-t", *ssh_target(name), f"bash -lc 'cd {repo} && exec {tool}'"])
    if tool == "ssh":
        os.execvp("ssh", ["ssh", "-t", *ssh_target(name), f"cd {repo} && exec bash -l"])
    if tool not in EDITORS:
        sys.exit(f"{tool}: open one of {', '.join([*sorted(commands), *EDITORS])}")
    if not ssh_config_installed():
        sys.exit(f"{tool} finds sbx-{name} through ~/.ssh/config: run `sbx ssh-config` once")
    host = f"sbx-{name}"
    commands = {
        "code": ["code", "--remote", f"ssh-remote+{host}", repo],
        "cursor": ["cursor", "--remote", f"ssh-remote+{host}", repo],
        "zed": ["zed", f"ssh://{host}{repo}"],
    }
    os.execvp(commands[tool][0], commands[tool])


def runtimes_cmd(args):
    """sbx runtimes [available | test | add | set | remove | default KIND]: list, or change (admins)."""
    client = Client(CONTROL)
    if args.action == "available":
        return runtimes_available(args, client)
    if args.action == "test":
        return runtime_test(args, client)
    if args.action:
        return runtime_change(args, client)
    found = client.api("GET", "/api/runtimes")
    if args.json:
        print(json.dumps(found))
        return
    if not found["runtimes"]:
        print("no sandbox runtime is set up: sbx runtimes available, then sbx runtimes add KIND")
    for r in found["runtimes"]:
        where = "this server" if r.get("host") else "hosted"
        plan = f" ({r['plan']['title']}{' plan' if r['plan']['name'] != 'custom' else ''})" if r.get("plan") else ""
        print(f"{r['kind']:<12} {'default' if r.get('default') else '':<8} {where:<12} "
              f"{r['status']['state']:<14} {r['title']}{plan}: {r['status']['summary']}")


def runtimes_available(args, client):
    """Every runtime the server's packages provide: whether it can run here, its plans, its settings now."""
    found = client.api("GET", "/api/runtimes/available")
    if args.json:
        print(json.dumps(found))
        return
    for r in found:
        state = "enabled" + (", default" if r.get("default") else "") if r.get("enabled") else (
            "can be added" if r.get("can_run") and not r.get("host") else
            f"can't run here: {r['why_not']}" if not r.get("can_run") else "set up by install.sh")
        print(f"{r['kind']:<12} {r['title']}: {state}")
        if r.get("edit_blocker") and not r.get("host"):
            print(f"  {r['edit_blocker']['message']}")
        if r.get("plan"):
            print(f"  plan now: {r['plan']['name']}")
        for p in r.get("presets") or []:
            fixed = ", ".join(f"{k}={v}" for k, v in p["values"].items() if k not in (p.get("adjustable") or []))
            free = ", ".join(f"{k} (from {p['values'][k]})" for k in p.get("adjustable") or [])
            print(f"  --plan {p['name']:<8} {fixed}{'; adjustable: ' + free if free else ''}")
        if r.get("presets"):
            print("  --plan custom   every limit with --set")
        for s in r.get("settings") or []:
            if s.get("secret"):
                now = "set" if s["key"] in (r.get("secrets_set") or []) else "not set"
                print(f"  --secret {s['key']} ({s['label']}: {now}; the value on stdin)")
                continue
            span = f"; {s['min']}..{s['max']}" if s.get("min") is not None and s.get("max") is not None else ""
            unit = f" {s['unit']}" if s.get("unit") else ""
            now = (r.get("values") or {}).get(s["key"]) or f"default {s['default'] or 'none'}"
            print(f"  --set {s['key']}=… ({s['label']}{unit}{span}; now {now})")
        if r.get("enabled") and r.get("remove_blocker"):
            print(f"  can't remove: {r['remove_blocker']['message']}")


def check_line(c) -> str:
    """A failed check in one line: its name, what was found, and the how-to page when it has one."""
    return f"{c.get('name')}: {c.get('detail')}" + (f" (how to: {c['help_url']})" if c.get("help_url") else "")


def runtime_test(args, client):
    """sbx runtimes test KIND: the runtime's checks, as Settings and sbx-admin doctor run them; non-zero when any fails."""
    if not args.kind:
        sys.exit("sbx runtimes test KIND (sbx runtimes lists the enabled ones)")
    found = client.api("POST", f"/api/runtimes/{urllib.parse.quote(args.kind, safe='')}/test")
    if args.json:
        print(json.dumps(found))
    else:
        for c in found["checks"]:
            print(f"{'ok ' if c['ok'] else 'NO '} {c['name']}: {c['detail']}"
                  + (f" — {c['fix']}" if not c["ok"] and c.get("fix") else ""))
            if not c["ok"] and c.get("help_url"):
                print(f"    how to: {c['help_url']}")
    if not found["ok"]:
        sys.exit(1)


def _read_secret(prompt):
    """One line of stdin (hidden at a terminal), never argv or the environment."""
    value = getpass.getpass(f"{prompt} (hidden): ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    return value.strip()


def _runtime_body(args, runtime, adding):
    """What a change sends: --plan (without one, adding takes the plan the provider says the key's account
    is on, else the runtime's default limits; the server resolves its limits),
    each --set, and each --secret read from stdin (one line each, in order; hidden at a terminal): a secret
    never on the command line."""
    settings = {s["key"]: s for s in runtime.get("settings") or []}
    values = {}
    for pair in args.set or []:
        key, sep, value = pair.partition("=")
        if not sep:
            sys.exit(f"--set {pair}: expected KEY=VALUE")
        if (settings.get(key) or {}).get("secret"):
            sys.exit(f"{key} is a secret: --secret {key} reads it from stdin, never the command line")
        values[key] = value
    for key in args.secret or []:
        setting = settings.get(key)
        if setting is None or not setting.get("secret"):
            sys.exit(f"{key} is no secret of {runtime['title']}; sbx runtimes available lists them")
        value = _read_secret(f"{setting['label']} for {runtime['title']}")
        if not value:
            sys.exit(f"no value for {key} on stdin")
        values[key] = value
    return {"plan": args.plan, "values": values} if args.plan else {"values": values}


def runtime_change(args, client):
    """sbx runtimes add | set | remove | default KIND (and sbx setup runtime add):
    queued by sbx-control, applied by the host, which restarts sbx-control;
    waits for it unless --no-wait."""
    if not args.kind:
        sys.exit(f"sbx runtimes {args.action} KIND (sbx runtimes available lists them)")
    kind = urllib.parse.quote(args.kind, safe="")
    if args.action in ("add", "set"):
        runtime = next((r for r in client.api("GET", "/api/runtimes/available") if r["kind"] == args.kind), None)
        if runtime is None:
            sys.exit(f"there is no runtime {args.kind}: sbx runtimes available lists them")
        if runtime.get("edit_blocker") and args.action == "add":
            sys.exit(runtime["edit_blocker"]["message"])
        body = _runtime_body(args, runtime, adding=args.action == "add")
        queued = (client.api("POST", "/api/runtimes", {"kind": args.kind, **body}) if args.action == "add" else
                  client.api("PUT", f"/api/runtimes/{kind}", body))
    elif args.action == "remove":
        queued = client.api("DELETE", f"/api/runtimes/{kind}")
    else:
        queued = client.api("PUT", "/api/runtimes/default", {"kind": args.kind})
    if args.no_wait:
        print(json.dumps(queued) if args.json else f"queued: {args.action} {args.kind} (sbx runtimes to see it once applied)")
        return
    done = wait_host_request(client, "/api/runtimes/change", queued, args.timeout, "the runtime change")
    print(json.dumps(done) if args.json else done.get("message") or "done")


# A host request's state (host_requests on the server), as GET /api/runtimes/change answers it.
HOST_REQUEST_STATES = {"queued": "queued; waiting for the host to apply it", "applying": "applying",
                       "done": "applied; waiting for sbx-control to restart"}


def wait_host_request(client, path, first, timeout, what):
    """A request the host applies, once it is done and a restarted sbx-control answers (as the web app
    follows it); a poll the restart fails is retried. Exits non-zero when it failed."""
    def verdict(change):
        state = change.get("state")
        if state == "failed":
            return None, f"{what} failed: {change.get('message') or 'see the server log'}"
        done = state == "done" and not change.get("restarting")
        return (None if done else HOST_REQUEST_STATES.get(state, state)), done
    return follow(lambda: client.request("GET", path, timeout=30), verdict, timeout, first=first,
                  retry_statuses=RETRIED,
                  late=lambda change: f"{what} is still {change.get('state')} after {timeout} s: sbx runtimes shows it "
                                      "once applied")


def endpoints_cmd(args):
    """sbx endpoints [ls | add | rm NAME | grant NAME REPO | revoke NAME REPO] (admins): this server's
    private services for sandboxes on runtimes that don't reach its private network, and which repos'
    sandboxes may use each; add and rm wait until the server applied the change."""
    if args.action in (None, "ls"):
        found = api("GET", "/api/endpoints")
        if args.json:
            print(json.dumps(found))
            return
        if not found["domain"]:
            print("no domain yet: endpoints need one (sbx setup address, or Settings)")
        for e in found["endpoints"]:
            used = f"; used by {', '.join(e['used_by'])}" if e["used_by"] else ""
            print(f"{e['name']:<20} {e['state']:<9} {e['address']} -> {e['target']} "
                  f"({e['server'] or 'internal host'}; for {', '.join(e.get('repos') or []) or 'no repo yet'}){used}"
                  + (f": {e['message']}" if e["message"] else ""))
        for c in found["candidates"]:
            print(f"{'':<20} {'off':<9} managed server {c['server']} ({c['kind']}): sbx endpoints add --server {c['server']}")
        if not found["endpoints"] and not found["candidates"]:
            print("no endpoints" + ("" if found["needed"] else " (no enabled runtime needs one)"))
        return
    if args.action in ("grant", "revoke"):
        if not args.endpoint or not args.repo_name:
            sys.exit(f"sbx endpoints {args.action} NAME OWNER/REPO")
        path = f"/api/endpoints/{urllib.parse.quote(args.endpoint, safe='')}/repos"
        repo = args.repo_name.lower()
        changed = (api("POST", path, {"repo": repo}) if args.action == "grant" else
                   api("DELETE", f"{path}/{urllib.parse.quote(repo, safe='/')}"))
        print(json.dumps(changed) if args.json else
              f"{changed['name']}: for {', '.join(changed['repos']) or 'no repo'}")
        return
    if args.action == "add":
        if bool(args.server) == bool(args.target):
            sys.exit("sbx endpoints add --server NAME, or --name NAME --target HOST:PORT [--repo OWNER/REPO ...]")
        body = {"server": args.server, "name": args.name, "target": args.target, "repos": args.repo}
        added = api("POST", "/api/endpoints", {k: v for k, v in body.items() if v})
        name = added["name"]
    else:
        if not args.endpoint:
            sys.exit("sbx endpoints rm NAME (sbx endpoints lists them)")
        name = args.endpoint
        added = api("DELETE", f"/api/endpoints/{urllib.parse.quote(name, safe='')}")
    if args.no_wait:
        print(json.dumps(added) if args.json else f"{args.action}: {name}, applying (sbx endpoints shows it)")
        return
    done = wait_endpoints(name, removed=args.action == "rm", timeout=args.timeout)
    print(json.dumps(done) if args.json else (f"{name}: on, at {done['address']}" if done else f"{name}: removed"))


def wait_endpoints(name, removed, timeout, interval=2.0):
    """The endpoint once the server applied it (None once a removal is applied);
    exits non-zero when it failed or the wait ran out."""
    def verdict(found):
        endpoint = next((e for e in found["endpoints"] if e["name"] == name), None)
        if removed:
            if endpoint is None and not found.get("applying"):
                return None, (f"removing {name} failed: {found['failure']}" if found.get("failure") else True)
        elif endpoint is None:
            return None, f"the endpoint {name} is gone"
        elif endpoint["state"] == "on":
            return None, True
        elif endpoint["state"] == "failed":
            return None, f"the endpoint {name} failed: {endpoint['message'] or 'see the server log'}"
        return "applying on the server", False
    found = follow(lambda: api("GET", "/api/endpoints"), verdict, timeout, interval=interval,
                   late=f"the endpoint {name} is still applying after {timeout} s: sbx endpoints shows it once applied")
    return next((e for e in found["endpoints"] if e["name"] == name), None)


# sbx secrets: ask the owner for keys on the server's keys page, and follow the request


DURATION = re.compile(r"^(\d+)([smhd]?)$")
DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def duration(text):
    """30m, 24h, 7d (or seconds) as seconds."""
    found = DURATION.match((text or "").strip().lower())
    if not found:
        sys.exit(f"--expires {text}: a duration such as 30m, 24h or 7d")
    return int(found.group(1)) * DURATION_UNITS[found.group(2)]


def secrets_client(args):
    """The first-run setup link's client when one is given (an agent setting up a fresh server), else the
    signed-in session's."""
    if getattr(args, "link_file", None) or os.environ.get("SBX_SETUP_LINK"):
        return setup_client(args)
    return Client(CONTROL)


def secrets_cmd(args):
    """sbx secrets request | status | ls | cancel | submit (admins; request, status, ls and cancel also with
    the setup link)."""
    # Submitting takes an admin signed in as themselves; the setup link is refused there.
    client = Client(CONTROL) if args.action == "submit" else secrets_client(args)
    if args.action == "request":
        return secrets_request(args, client)
    if args.action == "ls":
        found = client.api("GET", "/api/secret-requests")["requests"]
        if args.json:
            print(json.dumps(found))
            return
        for r in found:
            print(f"{r['id']}  {r['state']:<9} {len(r['items']) - r['remaining']}/{len(r['items'])} in  "
                  f"expires {r['expires_at']}  {r['url']}")
        if not found:
            print("no key requests")
        return
    if not args.args:
        sys.exit(f"sbx secrets {args.action} ID (sbx secrets ls lists them)")
    path = f"/api/secret-requests/{urllib.parse.quote(args.args[0], safe='')}"
    if args.action == "status":
        found = client.api("GET", path)
        if args.wait:
            found = wait_secrets(client, path, found, args.timeout)
        print(json.dumps(found) if args.json else secrets_summary(found))
        if found["state"] in ("expired", "cancelled") or _applying(found) == "failed":
            sys.exit(1)
        return
    if args.action == "cancel":
        found = client.api("DELETE", path)
        print(json.dumps(found) if args.json else f"{found['id']}: cancelled")
        return
    secrets_submit(args, client, path)


def secrets_request(args, client):
    """sbx secrets request TARGET LABEL [TARGET LABEL ...]: the keys page's URL for the owner; with --wait,
    until every key is in (non-zero when the request expires or is cancelled)."""
    pairs = args.args
    if not pairs or len(pairs) % 2:
        sys.exit("sbx secrets request TARGET LABEL [TARGET LABEL ...]: each target with its label, e.g.\n"
                 '  sbx secrets request runtime:boat "Boat API key" dns:cloudflare "Cloudflare DNS token"')
    helps = {}
    for pair in args.help_url or []:
        target, sep, url = pair.partition("=https://")
        if not sep:
            sys.exit(f"--help-url {pair}: expected TARGET=https://...")
        helps[target] = "https://" + url
    items = [{"target": t, "label": label, **({"help_url": helps.pop(t)} if t in helps else {})}
             for t, label in zip(pairs[::2], pairs[1::2])]
    if helps:
        sys.exit(f"--help-url names targets the request does not ask for: {', '.join(helps)}")
    body = {"items": items, "expires_in": duration(args.expires), **({"note": args.note} if args.note else {})}
    found = client.api("POST", "/api/secret-requests", body)
    said = f"send the owner this link (they sign in to use it; it expires {found['expires_at']})"
    if not args.wait:
        if args.json:
            print(json.dumps(found))
        else:
            print(found["url"])
            note(f"{said}; `sbx secrets status {found['id']} --wait` waits for the keys")
        return
    note(f"{said}: {found['url']}")
    if not args.json:
        print(found["url"])
    path = f"/api/secret-requests/{urllib.parse.quote(found['id'], safe='')}"
    found = wait_secrets(client, path, found, args.timeout)
    print(json.dumps(found) if args.json else secrets_summary(found))


def secrets_summary(found):
    total = len(found["items"])
    lines = [f"{found['id']}: {found['state']}, {total - found['remaining']}/{total} keys in "
             f"(expires {found['expires_at']})"]
    for i in found["items"]:
        applied = i.get("applied") or {}
        on_server = f" (on the server: {applied['state']}{': ' + applied['message'] if applied.get('message') else ''})" \
            if applied else ""
        lines.append(f"  {'in' if i['done'] else '..'}  {i['target']}: {i['label']}{on_server}")
    return "\n".join(lines)


# A key request's runtime keys, as the host applies them (runtime_changes on the server).
APPLYING = {"queued": "queued for the server", "applying": "being applied on the server",
            "restarting": "applied; sbx-control is restarting to take them"}


def _applying(found):
    """Where the server is with the request's runtime keys: none, queued, applying, restarting, done,
    failed or unknown."""
    return (found.get("runtime_changes") or {}).get("state") or "none"


def wait_secrets(client, path, first, timeout):
    """The request once every key is in and the server applied its runtime keys (one change for all of
    them, which restarts sbx-control); exits non-zero when it expires or is cancelled, when the server did
    not apply a runtime key, or past timeout (default: until it expires, and 10 minutes more for the
    runtime keys). A poll sbx-control's restart fails is retried."""
    def verdict(found):
        state = found["state"]
        if state in ("expired", "cancelled"):
            return None, f"the key request {found['id']} is {state}; {found['remaining']} key(s) never came"
        total = len(found["items"])
        applying = _applying(found)
        if applying == "failed":   # a runtime key submitted earlier, even while others are still to come
            return None, (f"the server did not apply the runtime keys: "
                          f"{found['runtime_changes'].get('message') or 'see sbx runtimes'}")
        if state != "completed":
            return f"waiting for the owner: {total - found['remaining']}/{total} keys in", False
        if applying in APPLYING:
            return f"every key is in; the runtime keys are {APPLYING[applying]}", False
        return None, True
    if timeout is None:
        try:
            ends = calendar.timegm(time.strptime(first["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
            timeout = max(0, ends - time.time()) + 600
        except (KeyError, ValueError):
            timeout = 86400
    return follow(lambda: client.request("GET", path, timeout=30), verdict, timeout, interval=3, first=first,
                  retry_statuses=RETRIED,
                  late=lambda found: f"still waiting for {found['remaining']} key(s) of {found['id']}: "
                                     f"sbx secrets status {found['id']} --wait")


def secrets_submit(args, client, path):
    """sbx secrets submit ID [TARGET ...]: the keys themselves, one line of stdin each, in order (hidden at a
    terminal), never on the command line; without TARGET, every key the request still asks for. A key that
    reuses one the server holds needs no value. Non-zero when any is refused."""
    found = client.api("GET", path)
    if found["state"] != "pending":
        sys.exit(f"{found['id']} is {found['state']}")
    wanted = args.args[1:] or [i["target"] for i in found["items"] if not i["done"] and not i["reuse"]]
    values = {}
    for target in wanted:
        item = next((i for i in found["items"] if i["target"] == target or i["target"].startswith(target + ":")), None)
        value = _read_secret(f"{item['label'] if item else target} ({target})")
        if not value:
            sys.exit(f"no value for {target} on stdin")
        values[target] = value
    done = client.api("POST", f"{path}/submit", {"values": values})
    if args.json:
        print(json.dumps(done))
    else:
        for r in done["results"]:
            print(f"{'ok ' if r['ok'] else 'NO '} {r['target']}" + ("" if r["ok"] else f": {r['error']}"))
        print(secrets_summary(done["request"]))
    if not done["ok"]:
        sys.exit(1)


def extensions_cmd(args):
    """sbx extensions [ls | set KIND | disconnect KIND] (admins): the Extensions page. set takes --set
    FIELD=VALUE and --secret FIELD (from stdin); a change to what every template holds says how many
    templates rebuild on each runtime, and with --wait waits until they are built (non-zero when one
    fails)."""
    if args.action in (None, "ls"):
        found = api("GET", "/api/connections")["connections"]
        if args.json:
            print(json.dumps(found))
            return
        for c in found:
            print(f"{c['kind']:<14} {c['state']:<14} {c['summary']}")
        return
    if not args.kind:
        sys.exit(f"sbx extensions {args.action} KIND (sbx extensions lists them)")
    kind = urllib.parse.quote(args.kind, safe="")
    if args.action == "set":
        current = next((c for c in api("GET", "/api/connections")["connections"] if c["kind"] == args.kind), None)
        if current is None:
            sys.exit(f"there is no extension {args.kind}: sbx extensions lists them")
        fields = {f["name"]: f for f in current.get("fields") or []}
        values = {}
        for pair in args.set or []:
            key, sep, value = pair.partition("=")
            if not sep:
                sys.exit(f"--set {pair}: expected FIELD=VALUE")
            if key not in fields:
                sys.exit(f"{current['title']} has no field {key}; it has: {', '.join(fields) or 'none'}")
            if fields[key].get("secret"):
                sys.exit(f"{key} is a secret: --secret {key} reads it from stdin, never the command line")
            values[key] = value
        for key in args.secret or []:
            if not (fields.get(key) or {}).get("secret"):
                sys.exit(f"{key} is no secret field of {current['title']}")
            value = _read_secret(f"{fields[key]['label']} for {current['title']}")
            if not value:
                sys.exit(f"no value for {key} on stdin")
            values[key] = value
        changed = api("PUT", f"/api/connections/{kind}", {"values": values})
    else:
        changed = api("DELETE", f"/api/connections/{kind}")
    report = changed.get("rebuilds")
    built = wait_rebuilds(report, args.timeout) if args.wait else None
    if args.json:
        print(json.dumps({**changed, **({"templates": built} if built is not None else {})}))
        return
    print(f"{changed['title']}: {changed['summary']}")
    if report:
        print(report["message"])
    if built is not None:
        print("the templates are rebuilt; new sandboxes start from them")


def wait_rebuilds(report, timeout, interval=10):
    """The rebuilds a change started (report: its rebuilds), followed until each template is built as
    planned now; exits non-zero when one fails. None when nothing rebuilds now."""
    wanted = (report or {}).get("keys") or {}
    if not (report or {}).get("building") or not wanted:
        return None
    total = sum(len(keys) for keys in wanted.values())

    def verdict(found):
        rows = {(kind, r["key"]): r for kind, listed in found["runtimes"].items() for r in listed}
        left = 0
        for kind, keys in wanted.items():
            for key in keys:
                row = rows.get((kind, key))
                if row is None:   # nothing starts from it any more
                    continue
                if row["phase"] == "stuck":
                    return None, f"the {key} template failed on {kind}: {row.get('error') or 'sbx templates shows it'}"
                if row["phase"] == "unplanned":
                    return None, f"the {key} template on {kind} can't be planned: sbx templates shows it"
                left += row["phase"] != "current"
        return (f"rebuilding templates: {total - left} of {total} built" if left else None), not left
    return follow(lambda: request("GET", "/api/templates", timeout=60), verdict, timeout, interval=interval,
                  retry_statuses=RETRIED,
                  late=f"the templates are still rebuilding after {timeout} s: sbx templates shows them")


def _minutes(seconds) -> str:
    return "under a minute" if seconds < 60 else f"about {round(seconds / 60)} min"


def template_step(kind, t) -> str:
    """A building template's progress in words: "sbx-base on embed: step 3 of 12 (RUN ...), about 4 min left"."""
    line = f"{t['key']} on {kind}"
    if t.get("steps"):
        line += f": step {t.get('step') or 0} of {t['steps']}" + (f" ({t['step_name']})" if t.get("step_name") else "")
    if t.get("eta_s") is not None:
        line += f", {_minutes(t['eta_s'])} left"
    return line


def templates_verdict(found):
    """follow()'s verdict on GET /api/templates: (progress, done): done once every template is built as
    planned now; why it failed (a text) when one failed, can't be planned, or none may build yet."""
    if found.get("blocked"):
        return None, f"no template builds yet: {found['blocked']}"
    rows = [(kind, t) for kind, listed in found["runtimes"].items() for t in listed]
    for kind, t in rows:
        if t["phase"] == "stuck":
            return None, f"the {t['key']} template failed on {kind}: {(t.get('error') or 'sbx templates shows it')[-300:]}"
        if t["phase"] == "unplanned":
            return None, f"the {t['key']} template on {kind} can't be planned: sbx templates shows it"
    left = [(kind, t) for kind, t in rows if t["phase"] != "current"]
    if not left:
        return None, True
    building = [template_step(kind, t) for kind, t in left if t["phase"] == "building"]
    progress = f"templates: {len(rows) - len(left)} of {len(rows)} built" + (f"; building {'; '.join(building)}"
                                                                             if building else "; waiting to build")
    return progress, False


def templates_cmd(args):
    """sbx templates (admins; the setup link while the setup is open): every template sandboxes start from,
    per runtime, and where its build stands; with --wait, followed until all are built."""
    signed_in = CONTROL and keychain_get(API_TOKEN_SERVICE)
    link = args.link_file or (not signed_in and os.environ.get("SBX_SETUP_LINK"))
    client = setup_client(args) if link else Client(CONTROL)
    fetch = lambda: client.request("GET", "/api/templates", timeout=60)   # noqa: E731
    if args.wait:
        found = follow(fetch, templates_verdict, args.timeout, interval=5, retry_statuses=RETRIED,
                       late=lambda found: f"the templates are not all built after {args.timeout} s "
                                          f"({templates_verdict(found)[0] or 'sbx templates shows them'})")
    else:
        found = client.api("GET", "/api/templates")
    if args.json:
        print(json.dumps(found))
        return
    for kind, listed in found["runtimes"].items():
        for t in listed:
            step = f"  step {t.get('step') or 0}/{t['steps']}" if t["phase"] == "building" and t.get("steps") else ""
            print(f"{kind:<10} {t['key']:<32} {t['phase']:<10} {t.get('template') or '-'}{step}"
                  + (f"  ({t['error'][-200:]})" if t.get("error") else ""))


# sbx setup: a fresh installation's first-run setup, without a browser, through the setup link


def _setup_link(args):
    if args.link_file:
        return pathlib.Path(args.link_file).expanduser().read_text().strip()
    return os.environ.get("SBX_SETUP_LINK", "").strip()


def signed_in_client(args):
    """For sbx setup finish without --admin: the admin `sbx login` signed in (its API token), so neither a
    password nor the setup link is needed; None when not signed in, or signed in to another server than
    the link's."""
    if args.admin or not CONTROL or not keychain_get(API_TOKEN_SERVICE):
        return None
    link = _setup_link(args)
    if link and urllib.parse.urlsplit(link)[:2] != urllib.parse.urlsplit(CONTROL)[:2]:   # scheme and authority
        return None
    return Client(CONTROL)


def setup_client(args):
    """sbx-control as the setup link names it, from SBX_SETUP_LINK or --link-file (never argv: it is a
    credential, and stdin is the secrets'), signed in with its token."""
    link = _setup_link(args)
    if not link:
        sys.exit("the onboarding link install.sh printed last (or sbx-admin setup-link): in SBX_SETUP_LINK, or a file named "
                 "by --link-file; never on the command line")
    url = urllib.parse.urlsplit(link)
    token = urllib.parse.parse_qs(url.fragment).get("t", [""])[0]
    # http only to this machine itself (a tunnel): the token never crosses a network in plain text.
    if not tls_or_loopback(link) or not url.netloc or not token:
        sys.exit("that is not a setup link: https://HOST/app/setup#t=TOKEN")
    context = None
    if args.insecure:
        import ssl
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return Client(f"{url.scheme}://{url.netloc}", token, context)


def setup_cmd(args):
    client = (args.step == "finish" and signed_in_client(args)) or setup_client(args)
    {"status": setup_status, "address": setup_address, "runtime": setup_runtime, "admin": setup_admin,
     "agents": setup_agents, "finish": setup_finish}[args.step](args, client)


def _steps(state):
    return {s["step"]: s for s in state.get("steps") or []}


def setup_status(args, client):
    state = client.api("GET", "/api/setup")
    if args.json:
        print(json.dumps(state))
        return
    if state.get("complete"):
        print(f"set up: {state['access']['url']} (sign in, then: sbx login)")
        return
    names = {"address": "address", "runtime": "runtime", "sign_in": "admin", "agents": "agents"}
    for step in ("address", "sign_in", "runtime", "agents"):
        found = _steps(state).get(step)
        if found:
            print(f"{'done' if found['done'] else 'todo':<5} {names[step]:<8} {found['summary']}")
    print(f"server address: {state['host'].get('address') or 'unknown'}; link: {state.get('link', '')}")
    print("ready to finish: sbx setup finish" if state.get("can_complete") else
          "next: sbx setup address | runtime add | admin, then sbx setup finish")


def setup_agents(args, client):
    """Optional: the coding agents whose connections to suggest (none is a
    choice); every template has every agent whatever is chosen."""
    if not args.kinds:
        state = client.api("GET", "/api/setup")
        if args.json:
            print(json.dumps(state.get("agents") or []))
            return
        for agent in state.get("agents") or []:
            print(f"{'chosen' if agent['chosen'] else '-':<7} {agent['kind']:<12} {agent['title']}")
        print("choose: sbx setup agents KIND [KIND ...], or sbx setup agents none")
        return
    kinds = [] if [k.lower() for k in args.kinds] == ["none"] else args.kinds
    state = client.api("PUT", "/api/setup/agents", {"agents": kinds})
    if args.json:
        print(json.dumps(state))
        return
    print(f"coding agents: {_steps(state).get('agents', {}).get('summary') or 'none'} (optional; connect each "
          "one later under Extensions)")


def setup_address(args, client):
    """IP access or a domain (its certificate follows on its own), then waits until HTTPS works there."""
    state = client.api("GET", "/api/setup")
    if args.domain:
        values = {"name": args.domain, "address": state["host"].get("address", ""), "acme": args.dns,
                  "acme_email": args.email or ""}
        for key in args.secret or []:   # the DNS provider's credentials (cloudflare_api_token, aws_...)
            value = _read_secret(key)
            if not value:
                sys.exit(f"no value for {key} on stdin")
            values[key] = value
        client.api("PUT", "/api/connections/domain", {"values": values})
    else:
        address = args.ip or state["host"].get("address", "")
        if not address:
            sys.exit("the server's address is unknown: give it (sbx setup address --ip ADDRESS)")
        client.api("POST", "/api/setup/address", {"address": address})
    if args.no_wait:
        return

    def verdict(state):
        if _steps(state).get("address", {}).get("done"):
            return None, True
        error = state["access"].get("certificate_error")
        if error and args.domain:   # saving the domain cleared the last one: this attempt failed, and nothing retries soon
            return None, f"the certificate failed: {error}\nfix it, then run the same command again (it retries)"
        return f"waiting for the certificate{': ' + error if error else ''}", False
    state = follow(lambda: client.api("GET", "/api/setup"), verdict, args.timeout, interval=5,
                   late=lambda state: f"HTTPS is not ready after {args.timeout} s "
                                      f"({state['access'].get('certificate_error') or 'no certificate yet'}); "
                                      "sbx setup status")
    print(f"address ready: {state['access']['url']}" + (
        f"; the setup page is now at {state['link']} (the link's token still works)" if args.domain else ""))


def setup_runtime(args, client):
    if args.action == "available":
        return runtimes_available(args, client)
    return runtime_change(args, client)


def setup_admin(args, client):
    """The first admin's own account: the email, their password on stdin."""
    password = _read_secret(f"password for {args.email}")
    if len(password) < 8:
        sys.exit("the password comes on stdin: at least 8 characters")
    client.api("POST", "/api/setup/admin", {"email": args.email, "password": password})
    print(f"{args.email} has an account and is an admin; next: sbx login as them, then sbx setup "
          f"finish (or sbx setup finish --admin {args.email}, their password on stdin again)")


def setup_finish(args, client):
    """Finish the setup: as the admin `sbx login` signed in (no password), or with the setup link and
    --admin, as that admin (their own password on stdin)."""
    body = None
    if args.admin:
        password = _read_secret(f"password for {args.admin}")
        if not password:
            sys.exit("the admin's password comes on stdin")
        body = {"email": args.admin, "password": password}
    state = client.api("POST", "/api/setup/complete", body)
    if args.json:
        print(json.dumps(state))
        return
    print(f"set up: {state['access']['url']}; the setup link no longer works. Templates build on their own "
          "(about 10 minutes the first time; sbx templates). Next, if not signed in yet: sbx login")


def ls(args):
    found = api("GET", "/api/sandboxes")
    if args.json:
        print(json.dumps(found))
        return
    for info in found:
        print(f"sbx-{info['name']:<24} {info['branch']:<36} {info.get('readiness', '-'):<15} "
              f"{info['urls'].get('web', '-')}")


def urls_cmd(args):
    print_urls(api("GET", f"/api/sandboxes/{args.name}"), args.name)


def logs(args):
    paths = api("GET", f"/api/sandboxes/{args.name}").get("logs") or []
    if not paths:
        sys.exit("the app names no logs (logs: in its .sbx/sbx.yaml)")
    os.execvp("ssh", ["ssh", *ssh_target(args.name), f"tail -n 100 -F {' '.join(paths)}"])


def push(args):
    checkout = find_repo() or sys.exit("run this inside your checkout")
    branch = args.branch or current_branch(checkout)
    target = ssh_target(args.name)
    repo = api("GET", f"/api/sandboxes/{args.name}")["repo"]
    subprocess.run(["git", "-C", checkout, "push", "-q", "--force", f"{target[-1]}:{repo}",
                    f"{branch}:refs/heads/{branch}"],
                   env={**os.environ, "GIT_SSH_COMMAND": shlex.join(["ssh", *target[:-1]])}, check=True)
    print(f"pushed {branch} to sbx-{args.name}")


DOWN_TIMEOUT_SECONDS = 45 * 60


def down(args):
    """Delete the sandbox. The recipe's .sbx/cleanup runs first, on the
    server; what it printed goes to stderr. Its failure does not stop the
    delete, so it is a warning, not the exit status (--json has it all)."""
    # The server answers once its cleanup ran: a paused sandbox's resume (a
    # provider's can take up to 25 minutes) and the hook's 5 come first.
    timeout = getattr(args, "timeout", None) or DOWN_TIMEOUT_SECONDS
    with ticking(f"deleting sbx-{args.name}: waiting for the server", timeout):
        result = api("DELETE", f"/api/sandboxes/{args.name}", timeout=timeout,
                     late=f"no answer from sbx-control (none within {int(timeout)} s, or the "
                          f"connection closed before it), so whether "
                          f"sbx-{args.name} was deleted is unknown: check `sbx ls`") or {}
    forget_host(args.name)
    if args.json:
        print(json.dumps(result))
        return
    cleanup = result.get("cleanup")
    if cleanup:
        if cleanup.get("output", "").strip():
            note(cleanup["output"].rstrip())
        if cleanup.get("error") or cleanup.get("exit_code"):
            why = cleanup.get("error") or f"it exited with {cleanup['exit_code']}"
            note(f"warning: .sbx/cleanup failed ({why}); the sandbox was deleted anyway")
    print(f"sbx-{args.name} deleted")


def sandbox_cmd(args):
    """The operations agents use; each prints the sandbox's JSON."""
    path = f"/api/sandboxes/{args.name}"
    if args.action == "exec":
        script = sys.stdin.read()
        r = api("POST", f"{path}/exec", {"script": script, "timeout": args.timeout}, timeout=args.timeout + 60)
        sys.stdout.write(r["stdout"])
        sys.stderr.write(r["stderr"])
        sys.exit(r["exit_code"])
    if args.action == "resources":
        print(json.dumps(api("GET", f"{path}/resources?probe=true")))
        return
    if args.action == "pause":
        print(json.dumps(api("POST", f"{path}/pause")))
        return
    if args.action == "resume":
        api("POST", f"{path}/resume?start_app={'false' if args.no_app else 'true'}")
    elif args.action in ("restart", "prepare"):
        api("POST", f"{path}/{args.action}")
    if args.action == "info" and not args.wait:
        info = api("GET", path)
    else:
        info = wait_for(args.name, args.timeout)
    print(json.dumps(info))
    sys.exit(1 if failed(info) else 0)


def update(_args):
    """Install the version sbx-control serves next to the current one and
    switch to it; older versions stay for a rollback (ln -sfn …/<old> …/current)."""
    if VERSION == "dev":
        sys.exit("this sbx runs from a repo checkout; update with git pull")
    with urllib.request.urlopen(f"{CONTROL}/dist/version", timeout=30) as response:
        latest = response.read().decode().strip()
    if latest == VERSION:
        print(f"sbx {VERSION} is current")
        return
    with urllib.request.urlopen(f"{CONTROL}/dist/sbx.tgz", timeout=60) as response:
        tarball = response.read()
    with urllib.request.urlopen(f"{CONTROL}/dist/sbx.tgz.sha256", timeout=30) as response:
        expected = response.read().decode().strip()
    install(tarball, expected, latest)
    print(f"sbx {VERSION} -> {latest}")


def install(tarball, expected, version, root=INSTALL_ROOT):
    if hashlib.sha256(tarball).hexdigest() != expected:
        sys.exit("the download does not match its sha256; nothing changed")
    target = root / version
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        members = archive.getmembers()
        if any(m.name.startswith("/") or ".." in pathlib.PurePosixPath(m.name).parts or not (m.isfile() or m.isdir())
               for m in members):
            sys.exit("the release holds a path outside its folder or a link; nothing changed")
        target.mkdir(parents=True, exist_ok=True)
        archive.extractall(target, members)
    if (target / "VERSION").read_text().strip() != version or not (target / "sbx.py").exists():
        sys.exit(f"the release in {target} is not sbx {version}; nothing changed")
    tmp = root / "current.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    tmp.replace(root / "current")


def doctor(_args):
    """Check what sbx needs on this Mac; exit 1 if something is missing."""
    problems = 0

    def check(ok, label, fix=""):
        nonlocal problems
        print(f"{'ok ' if ok else 'NO '} {label}" + ("" if ok or not fix else f"  -> {fix}"))
        problems += not ok

    check(True, f"sbx {VERSION} for {CONTROL}")
    on_path = str(HOME / ".local/bin") in os.environ.get("PATH", "").split(":")
    check(on_path, "~/.local/bin on PATH", "open a new terminal, or add it to ~/.zshrc")
    check(bool(websocat()), "websocat", "brew install websocat")
    check(bool(public_keys()), "SSH key", "ssh-keygen -t ed25519")
    try:
        email = request("GET", "/api/me")["email"]
        error = ""
    except Refused as refused:
        email, error = None, str(refused)
    check(bool(email), f"signed in{' as ' + email if email else ''}", error or "sbx login")
    check(ssh_config_installed(), "~/.ssh/config includes the sandboxes (optional)", "sbx ssh-config")
    for kind, agent in (agents() if email else {}).items():
        check(agent_login(kind, agent) is not None, f"{agent['title']} login (optional)", f"sbx login {kind}")
    if email:
        try:
            mine = request("GET", "/api/sandboxes")
        except Refused as refused:
            check(False, "your sandboxes", str(refused))
            mine = []
        for info in mine:
            name = info["name"]
            if not (SSH_DIR / f"{name}.conf").exists():
                continue
            reached = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", *ssh_target(name),
                                      "true"], capture_output=True).returncode == 0
            check(reached, f"SSH to sbx-{name}", f"sbx down {name} && sbx up {name}")
    sys.exit(1 if problems else 0)


def run_agent(agent, brief, cwd=None):
    binary = shutil.which(agent)
    if not binary:
        print(brief)
        sys.exit(f"no {agent} on PATH; give your agent the brief above")
    if cwd:
        os.chdir(cwd)
    os.execv(binary, [binary, brief])


def recipe_files(folder):
    """A recipe folder's files as the API takes them: .sbx/<path> -> text,
    and the executable ones."""
    folder = pathlib.Path(folder)
    folder = folder / ".sbx" if (folder / ".sbx").is_dir() else folder
    if folder.is_symlink() or not (folder / "sbx.yaml").is_file():
        sys.exit(f"{folder}: no sbx.yaml (pass the checkout or its .sbx folder)")
    files, executable = {}, []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            sys.exit(f"{path} is a symlink; a recipe has regular files only")
        if not path.is_file():
            continue
        key = ".sbx/" + path.relative_to(folder).as_posix()
        try:
            files[key] = path.read_text()
        except UnicodeDecodeError:
            note(f"skipped {path}: not text")
            continue
        if os.access(path, os.X_OK):
            executable.append(key)
    return files, executable


def default_folder(explicit):
    return explicit or find_repo() or sys.exit("run this inside your checkout, or pass the recipe's folder")


def recipe_cmd(args):
    repo = this_repo(args.repo)
    if args.action == "rm":
        r = api("DELETE", f"/api/repos/{repo}/recipe")
        print(f"{repo}: the recipe installed with sbx recipe apply is removed; "
              + ("the default branch's .sbx/ applies now" if r["recipe"] == "imported" else "it needs a recipe now"))
        return
    if args.action == "pr" and args.status:
        found = api("GET", f"/api/repos/{repo}/setup-pr")
        pr, last = found["pull_request"], found["record"] or {}
        if args.json:
            print(json.dumps(found))
        else:
            if pr:
                print(recipe_pr_line(pr))
            if last.get("problem") or not pr:
                print(recipe_pr_line(last or None))
        if last.get("problem"):
            sys.exit(1)
        return
    files, executable = recipe_files(default_folder(args.dir))
    if args.action == "apply":
        r = api("PUT", f"/api/repos/{repo}/recipe", {"files": files, "executable": executable})
        print(f"{repo}: {r['files']} recipe file(s) installed on the server"
              + ("; it has a template of its own: sbx template build" if r["prebuilt"] else ""))
        return
    if not args.yes:
        out = sys.stderr if args.json else sys.stdout   # with --json, stdout holds the answer alone
        print(f"Open a draft setup PR on {repo} through the server's GitHub App:", file=out)
        for path in files:
            print(f"  {path}", file=out)
        print("Review these files first. Credentials belong on the sandbox server, not in the PR.", file=out)
        print("Publish this proposal? [y/N] ", end="", file=out, flush=True)
        if input().strip().lower() not in ("y", "yes"):
            return
    r = api("POST", f"/api/repos/{repo}/setup-pr", {"files": files, "executable": executable, "title": args.title})
    print(json.dumps(r) if args.json else recipe_pr_line(r))


def recipe_pr_line(pr):
    """The recipe's pull request (setup_pr's summary or record) in one line."""
    if not pr:
        return "no recipe pull request yet"
    if pr.get("problem"):
        return f"not published: {pr['problem']}"
    if pr.get("state") == "in_repository":
        return "the default branch has this recipe already; no pull request needed"
    word = "draft" if pr.get("draft") and pr.get("state") == "open" else pr.get("state") or "open"
    return f"{pr['url']} ({word})"


def checkout_paths(checkout):
    listed = subprocess.run(["git", "-C", checkout, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, text=True, check=True).stdout
    return [p for p in listed.split("\0") if p]


def scaffold(checkout, repo, prebuild):
    """Write the .sbx/ the server drafts from the checkout's files; the
    server names the files it needs to read."""
    body = {"paths": checkout_paths(checkout), "files": {}, "prebuild": prebuild}
    while True:
        r = api("POST", f"/api/repos/{repo}/scaffold", body)
        if "read" not in r:
            break
        if all(path in body["files"] for path in r["read"]):
            sys.exit(f"sbx-control keeps asking for {', '.join(r['read'])}")
        for path in r["read"]:
            try:
                body["files"][path] = (pathlib.Path(checkout) / path).read_text(errors="replace")
            except OSError:
                body["files"][path] = ""
    for path, text in r["files"].items():
        target = pathlib.Path(checkout) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        if path in r["executable"]:
            target.chmod(0o755)
    return r


def repo_cmd(args):
    repo = this_repo(args.repo)
    if args.action == "status":
        found = api("GET", f"/api/repos/{repo}")
        if args.json:
            print(json.dumps(found))
            return
        for key, value in found.items():
            if key == "recipe_pr":
                value = recipe_pr_line(value) + ("" if not value or value["current"] or value.get("problem")
                                                 else "; the recipe changed since")
            print(f"{key:<16} {', '.join(value) if isinstance(value, list) else value}")
        return
    if args.action == "brief":
        print(api("GET", f"/api/repos/{repo}/setup-brief", raw=True))
        return
    me = api("GET", "/api/me")
    status = api("GET", f"/api/repos/{repo}")
    if not status["trust"]:
        if not me["admin"]:
            sys.exit(f"{CONTROL} doesn't serve {repo}; ask an admin to run `sbx repo connect --repo {repo}`")
        api("POST", "/api/repo-onboarding/connect", {"repo": repo})
        status = api("GET", f"/api/repos/{repo}")
    print(f"{repo} on {CONTROL}: {status['trust']}, GitHub access: {status.get('github', '-')}")
    if status["trust"] == "trusted" and status.get("github") != "app" and status.get("github_app_install"):
        print(f"  for clones and pushes without keys, install the GitHub App on it: {status['github_app_install']}")
    checkout = args.dir or find_repo()
    if checkout and (github_repo(checkout) or "").lower() != repo:
        checkout = args.dir
    wanted = status["secrets_missing"]
    if checkout and not (pathlib.Path(checkout) / ".sbx" / "sbx.yaml").exists():
        r = scaffold(checkout, repo, args.prebuild)
        found = r["found"]
        print(f"wrote {', '.join(sorted(r['files']))}")
        print(f"  start: {found['dev'] or '(none found; the agent starts the app)'}; preview on port "
              f"{found['port'] or '(none found; the agent sets it)'}")
        wanted = found["secrets"]
    elif checkout:
        print(".sbx/ is already there; kept it")
    if status["trust"] != "trusted":
        print("public repos get no secrets or services here")
        return
    if wanted and not args.agent and sys.stdin.isatty():
        print(f"values it asks for: {', '.join(wanted)} (hidden; enter skips one)")
        for name in wanted:
            value = getpass.getpass(f"  {name}: ")
            if value:
                api("PUT", f"/api/repos/{repo}/env/{name}", {"value": value, "secret": True, "mine": not me["admin"]})
    if args.agent:
        print(f"starting {args.agent} with the setup brief; it writes .sbx/, tries it in a sandbox "
              "and verifies the application")
        run_agent(args.agent, api("GET", f"/api/repos/{repo}/setup-brief", raw=True), checkout)
    print("Next: give your agent the setup brief (sbx repo brief prints it), or sbx recipe apply, then sbx up check-a"
          + ("; it has a template of its own: sbx template build" if args.prebuild or status.get("prebuilt") else ""))


def template_cmd(args):
    repo = this_repo(args.repo)
    path = f"/api/repos/{repo}/template"
    if args.action == "rm":
        r = api("DELETE", path)
        print(f"{repo}: deleted {', '.join(r['deleted']) or 'no template'}"
              + (f"; kept {', '.join(r['kept_in_use'])} (sandboxes use it)" if r["kept_in_use"] else "")
              + ("; its recipe still asks for one, so it builds again" if r["rebuilds"] else ""))
        return
    if args.action == "build":
        api("POST", path)
        if not args.wait:
            print(f"building {repo}'s template on the server; sbx template status shows when it's done")
            return
    r = follow(lambda: api("GET", path), lambda r: (None, not args.wait or r["status"] != "building"), args.timeout,
               interval=5, late="the template is still building; sbx template status shows when it's done")
    r.pop("log", None)
    print(json.dumps(r))
    sys.exit(1 if r["status"] == "failed" else 0)


def read_dotenv(path):
    values = {}
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.removeprefix("export ").partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def applied(r):
    if r.get("pending"):
        return f"stored; delivery pending: {r.get('detail', 'the sandbox runtime did not answer')}"
    parts = []
    if r.get("running"):
        parts.append(f"{r['running']} running sandbox{'es' if r['running'] != 1 else ''} now")
    if r.get("paused"):
        parts.append(f"{r['paused']} paused when they next run")
    return "applies to " + (", ".join(parts) if parts else "no sandbox right now")


def installation_env(args):
    """sbx env --installation: values every served repo's installation
    variables may name as ${secret:NAME} (sbx-admin config set variables)."""
    if args.action == "ls":
        names = api("GET", "/api/secrets")
        if args.json:
            print(json.dumps(names, indent=2))
            return
        for row in names:
            print(f"{row['name']:<32} (secret)  set by {row['updated_by']} {row['updated_at']}")
        return
    if args.action == "rm":
        for name in args.args:
            print(f"removed {name}; {applied(api('DELETE', f'/api/secrets/{name}'))}")
        return
    if args.action != "set" or not args.args:
        sys.exit("sbx env ls|set NAME [VALUE]|rm NAME --installation")
    name = args.args[0]
    value = " ".join(args.args[1:]) if len(args.args) > 1 else (
        sys.stdin.read().rstrip("\n") if not sys.stdin.isatty() else getpass.getpass(f"{name} for this installation (hidden): "))
    if not value:
        sys.exit("no value; nothing set")
    print(f"set {name} (secret) for this installation; {applied(api('PUT', f'/api/secrets/{name}', {'value': value}))}")


def env_cmd(args):
    if args.installation:
        return installation_env(args)
    repo = this_repo(args.repo)
    base = f"/api/repos/{repo}/env"
    audiences = api("GET", "/api/me")["audiences"]
    if args.action == "ls":
        view = api("GET", base)
        if args.json:
            print(json.dumps(view, indent=2))
            return
        for row in view["variables"]:
            value = ("(secret)" if row.get("secret") else row.get("value", row.get("template", "(operator)")))
            who = "yours" if row["kind"] == "mine" else audiences.get(row.get("audience", "all"), "")
            agent = "" if row.get("agent", True) else "  hidden from agents"
            over = f"  (overridden by {row['overridden_by']})" if row.get("overridden_by") else ""
            print(f"{row['name']:<32} {str(value)[:48]:<48} {row['source']:<14} {who}{agent}{over}")
        for name in view["missing"]:
            print(f"{name:<32} {'(needs a value: sbx env set ' + name + ')':<48} sbx.yaml asks for it")
        return
    if args.action == "rm":
        for name in args.args:
            r = api("DELETE", f"{base}/{name}?mine={'true' if args.mine else 'false'}"
                              f"&restart={'false' if args.no_restart else 'true'}")
            print(f"removed {name}; {applied(r)}")
        return
    if args.action == "import":
        if len(args.args) != 1:
            sys.exit("sbx env import FILE (a .env file; - for stdin)")
        text = sys.stdin.read() if args.args[0] == "-" else pathlib.Path(args.args[0]).read_text()
        r = api("POST", f"{base}-import", {"text": text, "mine": args.mine, "restart": not args.no_restart,
                                           "audience": args.audience, "agent": not args.hide_from_agents})
        secret = [n for n, s in r["set"].items() if s]
        print(f"set {len(r['set'])} ({len(secret)} as secrets: {', '.join(secret) or 'none'}); {applied(r)}")
        return
    if not args.args:
        sys.exit("sbx env set NAME [VALUE]: without VALUE it asks (hidden), or reads stdin")
    name = args.args[0]
    if len(args.args) > 1:
        value = " ".join(args.args[1:])
    elif not sys.stdin.isatty():
        value = sys.stdin.read().rstrip("\n")
    else:
        prompt = f"{name} for {'your sandboxes of ' if args.mine else ''}{repo}"
        # Hidden unless --plain: the server keeps a new value secret.
        value = input(prompt + ": ") if args.plain else getpass.getpass(prompt + " (hidden): ")
    if not value:
        sys.exit("no value; nothing set")
    secret = False if args.plain else True if args.secret else None
    r = api("PUT", f"{base}/{name}", {"value": value, "secret": secret, "audience": args.audience,
                                      "agent": not args.hide_from_agents, "mine": args.mine,
                                      "restart": not args.no_restart})
    print(f"set {name}{' (secret)' if r['secret'] else ''} for {audiences.get(args.audience, args.audience)}"
          f"{' of yours' if args.mine else ''}; {applied(r)}")


def catalog_cmd(args):
    action = args.action or "ls"
    if action == "ls":
        found = api("GET", "/api/catalog" + (f"?kind={args.kind}" if args.kind else ""))["packages"]
        if args.json:
            print(json.dumps(found, indent=2))
            return
        for e in found:
            used = f"  used by {', '.join(e['used_by'])}" if e["used_by"] else ""
            print(f"{e['name']:<22} {e['kind']:<12} {e['title']:<28} {', '.join(e['inputs'])}{used}")
        return
    if action == "show":
        name = args.name or sys.exit("sbx catalog show NAME")
        e = api("GET", f"/api/catalog/{name}")
        print(f"{e['title']} ({e['kind']}, {e['source']})\n{e['description']}")
        for n, i in e["inputs"].items():
            about = i.get("help") or i.get("description") or ""
            choices = ", ".join(repr(c["value"]) for c in i.get("choices", ()))
            print(f"  input {n}{' (required)' if i['required'] else ''}{' secret' if i['secret'] else ''}: {about}"
                  + (f" (one of {choices})" if choices else ""))
        for n, o in (e.get("outputs") or {}).items():
            print(f"  output {n}{' (secret)' if o.get('sensitive') else ''}: ${{services.KEY.{n}}}")
        for var, value in (e.get("env") or {}).items():
            print(f"  sets {var}: {value}")
        for slot, r in e["requires"].items():
            # The saved connections of that kind (listed to admins): what connections: {slot: NAME} names.
            saved = [c["key"] for c in api("GET", f"/api/catalog/{r['kind']}").get("connections") or []]
            print(f"  requires {slot}: a {r['kind']} connection ({', '.join(f'{k} -> {v}' for k, v in r['expose'].items())})"
                  + (f"; saved: {', '.join(saved)}" if saved else ""))
        for c in e.get("connections") or []:
            print(f"  saved connection {c['key']}")
        if args.files:
            for path, text in api("GET", f"/api/catalog/{name}/files")["files"].items():
                print(f"\n--- {path}\n{text}")
        return
    if action == "new":
        name = args.name or sys.exit("sbx catalog new NAME --from PACKAGE")
        if not args.from_:
            sys.exit("start from a package the server has: sbx catalog new NAME --from PACKAGE (sbx catalog ls), "
                     "or let an agent write one: sbx catalog brief --what SERVICE --agent claude")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            sys.exit("a package name is lowercase letters, digits, - and _")
        folder = pathlib.Path(name)
        if folder.exists():
            sys.exit(f"{folder} exists")
        for path, text in api("GET", f"/api/catalog/{args.from_}/files")["files"].items():
            (folder / path).parent.mkdir(parents=True, exist_ok=True)
            (folder / path).write_text(text.replace(args.from_, name))
            if path.endswith(".sh"):
                (folder / path).chmod(0o755)
        print(f"{folder}/ copied from {args.from_}; edit package.yaml and the scripts, then: sbx catalog push ./{name}")
        return
    if action == "push":
        folder = pathlib.Path(args.name or ".")
        if not (folder / "package.yaml").exists():
            sys.exit(f"{folder}: no package.yaml")
        files = {str(p.relative_to(folder)): p.read_text() for p in sorted(folder.rglob("*"))
                 if p.is_file() and "__pycache__" not in p.parts}
        r = api("PUT", f"/api/catalog/{folder.resolve().name}", {"files": files, "message": args.message or ""})
        print(f"pushed {r['name']} ({r['kind']}); now: sbx catalog test {r['name']}")
        return
    if action == "test":
        name = args.name or sys.exit("sbx catalog test NAME [--input k=v ...] [--connection slot=key]")
        inputs = dict(x.split("=", 1) for x in (args.input or []))
        chosen = dict(x.split("=", 1) for x in (args.connection or []))
        r = api("POST", f"/api/catalog/{name}/test", {"inputs": inputs, "connections": chosen})
        for s in r["steps"]:
            print(f"  {'ok ' if s['ok'] else 'NO '} {s['step']}: {s['detail']}")
        for k, v in (r.get("outputs") or {}).items():
            print(f"  output {k} = {v}")
        print("passed" if r["ok"] else "failed")
        sys.exit(0 if r["ok"] else 1)
    what = args.what or "a service"
    name = args.name or re.sub(r"[^a-z0-9-]+", "-", what.lower()).strip("-") or "my-service"
    brief = api("GET", f"/api/catalog-brief?what={urllib.parse.quote(what)}&name={urllib.parse.quote(name)}", raw=True)
    if args.agent:
        print(f"starting {args.agent} with the package brief for {what}")
        run_agent(args.agent, brief)
    print(brief)


def attach_cmd(args):
    repo = this_repo(args.repo)
    spec = next((e for e in api("GET", "/api/catalog")["packages"] if e["name"] == args.package), None)
    if not spec:
        sys.exit(f"no package {args.package}; sbx catalog ls lists them")
    inputs = {}
    for pair in args.pairs:
        name, sep, value = pair.partition("=")
        if not sep:
            sys.exit(f"{pair}: expected name=value")
        inputs[name] = value
    if sys.stdin.isatty():
        for name, v in spec["inputs"].items():
            if name in inputs or not (v["required"] or v["secret"]):
                continue
            hint = v.get("default") or v.get("help") or ""
            value = (getpass.getpass if v["secret"] else input)(f"  {name}{f' [{hint}]' if hint else ''}: ").strip()
            if value:
                inputs[name] = value
    chosen = dict(x.split("=", 1) for x in (args.connection or []))
    r = api("POST", f"/api/repos/{repo}/attachments", {"package": args.package, "key": args.key or "", "inputs": inputs,
                                                      "connections": chosen})
    att = r.get("attachment") or {}
    print(f"{att.get('key', args.package)}: {att.get('state', '?')}" + (f" ({att['problem']})" if att.get("problem") else ""))


def detach_cmd(args):
    repo = this_repo(args.repo)
    r = api("DELETE", f"/api/repos/{repo}/attachments/{args.key}")
    print(f"{args.key}: {'disabled (declared in .sbx/sbx.yaml; remove it there)' if r.get('disabled') else 'removed'}")


def change_flags(p):
    """The flags of a runtime change (sbx runtimes add | set | remove | default, sbx setup runtime add)."""
    p.add_argument("--plan", help="add, set: a plan from sbx runtimes available (hobby, pro), or custom; add "
                   "without one takes the plan the provider says the key's account is on (Boat), else the "
                   "runtime's default limits (E2B Cloud: hobby). A Boat trial key is refused")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="add, set: a setting, in its unit (seconds, MiB); repeatable; a plan fixes its own limits")
    p.add_argument("--secret", action="append", metavar="KEY",
                   help="add, set: a secret setting, its value read from stdin (one line each), never argv")
    p.add_argument("--no-wait", action="store_true", help="return once the change is queued")
    p.add_argument("--timeout", type=seconds, default=600, help="seconds to wait for the change (default 600)")
    p.add_argument("--json", action="store_true", help="as the API answers (a change: its outcome)")


def parser():
    top = argparse.ArgumentParser(prog="sbx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(required=True, metavar="COMMAND")
    tools_help = "an agent the server offers (sbx login lists them), or code, cursor, zed, ssh"
    repo_help = "owner/name (default: the origin of the checkout you are in)"

    def command(name, func, help, **kwargs):
        p = sub.add_parser(name, help=help, description=help, **kwargs)
        p.set_defaults(func=func)
        return p

    p = command("login", login, "sign in to sbx-control; with AGENT (claude, codex, ...): store your own login "
                "for that agent on this Mac")
    p.add_argument("agent", nargs="?")
    p.add_argument("--timeout", type=seconds, default=300,
                   help="seconds to wait for the sign-in in the browser (default 300)")
    p = command("up", up, "a sandbox of the repo you are in, on your branch if GitHub has it; waits until it is ready")
    p.add_argument("name")
    p.add_argument("--repo", help=repo_help)
    p.add_argument("--branch", help="default: your current branch if pushed, else the repo's default")
    p.add_argument("--open", metavar="TOOL", help=f"then open this tool in it: {tools_help}")
    p.add_argument("--json", action="store_true", help="print the sandbox as the API returns it")
    p.add_argument("--no-wait", action="store_true", help="return once SSH works")
    p.add_argument("--timeout", type=seconds, default=1800, help="seconds to wait for it (default 1800)")
    p.add_argument("--runtime", metavar="KIND", help="the sandbox runtime it runs on (sbx runtimes lists them; "
                   "default: the repo's, else the server's)")
    p = command("runtimes", runtimes_cmd, "the sandbox runtimes the server runs sandboxes on; admins add, change "
                "and remove them (applied by the server, which restarts)")
    p.add_argument("action", nargs="?", choices=["available", "test", "add", "set", "remove", "default"],
                   help="none: list the enabled ones; available: what can run here, its plans and settings; "
                        "test: an enabled one's checks")
    p.add_argument("kind", nargs="?", help="test, add, set, remove, default: the runtime (e2b-cloud, ...)")
    change_flags(p)
    p = command("endpoints", endpoints_cmd, "admins: this server's private services (managed servers, internal "
                "hosts) for sandboxes on runtimes that don't reach its private network, over TLS at <name>.<domain>")
    p.add_argument("action", nargs="?", choices=["ls", "add", "rm", "grant", "revoke"],
                   help="none or ls: list them and the managed servers without one; add: --server NAME, or --name "
                        "NAME --target HOST:PORT --repo OWNER/REPO; rm NAME; grant | revoke NAME OWNER/REPO: which "
                        "repos' sandboxes may use an internal host's endpoint")
    p.add_argument("endpoint", nargs="?", metavar="NAME", help="rm, grant, revoke: the endpoint")
    p.add_argument("repo_name", nargs="?", metavar="REPO", help="grant, revoke: owner/name")
    p.add_argument("--repo", action="append", metavar="OWNER/REPO",
                   help="add, an internal host: a repo whose sandboxes may use it (repeatable)")
    p.add_argument("--server", help="add: a managed server (sbx-admin server ls)")
    p.add_argument("--name", help="add: the endpoint's name, a host name label (default for --server: its name)")
    p.add_argument("--target", metavar="HOST:PORT", help="add: an internal host within network.internal_cidrs")
    p.add_argument("--no-wait", action="store_true", help="add, rm: return once recorded")
    p.add_argument("--timeout", type=seconds, default=300, help="seconds to wait for the server to apply it (default 300)")
    p.add_argument("--json", action="store_true", help="as the API answers")
    p = command("extensions", extensions_cmd, "admins: the extensions (GitHub, coding agents) as the Extensions "
                "page shows them; set or disconnect one, and how many templates that rebuilds")
    p.add_argument("action", nargs="?", choices=["ls", "set", "disconnect"],
                   help="none or ls: list them; set KIND: save its fields (tested first); disconnect KIND: remove "
                        "its saved credentials and turn it off")
    p.add_argument("kind", nargs="?", metavar="KIND", help="set, disconnect: the extension (amp, claude-code, ...)")
    p.add_argument("--set", action="append", metavar="FIELD=VALUE", help="set: a non-secret field, FIELD=VALUE")
    p.add_argument("--secret", action="append", metavar="FIELD",
                   help="set: a secret field, its value read from stdin (one line each), never argv")
    p.add_argument("--wait", action="store_true",
                   help="set, disconnect: wait until the templates the change rebuilds are built; non-zero when one fails")
    p.add_argument("--timeout", type=seconds, default=3600, help="seconds to wait for the rebuilds (default 3600)")
    p.add_argument("--json", action="store_true", help="as the API answers (with --wait: and the templates then)")
    p = command("templates", templates_cmd, "admins (or the setup link while the setup is open): every template "
                "sandboxes start from, per runtime, and where its build stands")
    p.add_argument("--wait", action="store_true", help="follow the builds, a line each time their progress changes "
                   "(step k of n), until every template is built; non-zero when one fails or none may build yet")
    p.add_argument("--timeout", type=int, default=1800, help="seconds --wait waits in all (default 1800); non-zero "
                   "past it")
    p.add_argument("--json", action="store_true", help="as the API answers (with --wait: once all are built)")
    p.add_argument("--link-file", metavar="FILE", help="during the setup: a file holding the setup link (else "
                   "SBX_SETUP_LINK when not signed in)")
    p.add_argument("--insecure", action="store_true", help="with the setup link: accept the server's self-signed "
                   "certificate")

    p = command("secrets", secrets_cmd, "ask the owner for keys: a page on the server they sign in to, where each "
                "key goes straight to where it is used; and follow the request (admins)",
                epilog="sbx secrets request [--wait] TARGET LABEL [TARGET LABEL ...]\n"
                       "    TARGET: runtime:KIND[:KEY]  dns:cloudflare  connector:KIND[:FIELD]  repo:OWNER/NAME:VAR\n"
                       "    repo:OWNER/NAME:VAR=from:SOURCE reuses a key the server holds instead of asking\n"
                       "    (SOURCE: runtime:KIND[:KEY], dns:cloudflare, repo:OWNER/NAME:VAR, or another target)\n"
                       "sbx secrets status ID [--wait]       which keys are in (never a value)\n"
                       "sbx secrets ls | cancel ID\n"
                       "sbx secrets submit ID [TARGET ...]   the keys yourself, one line of stdin each\n"
                       "With SBX_SETUP_LINK or --link-file, request, status, ls and cancel use the first-run\n"
                       "setup link; keys are always submitted by a signed-in admin.",
                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["request", "status", "ls", "cancel", "submit"])
    p.add_argument("args", nargs="*", metavar="ARG")
    p.add_argument("--wait", action="store_true", help="request, status: until every key is in; non-zero when the "
                   "request expires or is cancelled")
    p.add_argument("--timeout", type=seconds, help="with --wait: seconds to wait (default: until the request expires, "
                   "and 10 minutes more for the server to apply runtime keys)")
    p.add_argument("--expires", default="24h", help="request: how long the page works (5m .. 7d; default 24h)")
    p.add_argument("--note", help="request: a line for the owner on the page")
    p.add_argument("--help-url", action="append", metavar="TARGET=URL",
                   help="request: where the owner learns to get that key (default: sbx's guide for it)")
    p.add_argument("--json", action="store_true", help="as the API answers")
    p.add_argument("--link-file", metavar="FILE", help="the first-run setup link's file (else SBX_SETUP_LINK)")
    p.add_argument("--insecure", action="store_true", help="with the setup link: accept a self-signed certificate")

    p = command("setup", setup_cmd, "a fresh server's first-run setup without a browser, through the onboarding link "
                "install.sh printed last (in SBX_SETUP_LINK or a --link-file, never argv); secrets on stdin")
    steps = p.add_subparsers(dest="step", required=True, metavar="STEP")
    link = argparse.ArgumentParser(add_help=False)
    link.add_argument("--link-file", metavar="FILE", help="a file holding the setup link (else SBX_SETUP_LINK)")
    link.add_argument("--insecure", action="store_true",
                      help="accept the self-signed certificate the server answers with before its own")
    s = steps.add_parser("status", parents=[link], help="each step and what is left")
    s.add_argument("--json", action="store_true")
    s = steps.add_parser("address", parents=[link], help="IP access, or a domain whose certificate follows; waits "
                         "until HTTPS works")
    s.add_argument("--ip", nargs="?", const="", metavar="ADDRESS", help="this server's IP (default: the one it sees)")
    s.add_argument("--domain", metavar="D", help="previews at <name>.D, the server at app.D")
    s.add_argument("--email", help="optional: a contact for the Let's Encrypt account (with --domain); it sends no expiry emails")
    s.add_argument("--dns", default="delegated", choices=["delegated", "cloudflare", "route53", "none"],
                   help="how the wildcard certificate is issued (default delegated: this server answers on port 53); "
                        "cloudflare, route53: a DNS API token, when port 53 can't open; none: self-signed, for a "
                        "first test (browsers warn, and only sbx setup takes it, with --insecure)")
    s.add_argument("--secret", action="append", metavar="KEY",
                   help="the DNS provider's credential (cloudflare_api_token, aws_access_key_id, ...) on stdin")
    s.add_argument("--no-wait", action="store_true")
    s.add_argument("--timeout", type=seconds, default=900, help="seconds to wait for HTTPS (default 900)")
    s = steps.add_parser("runtime", parents=[link], help="what can run here, and add the first sandbox runtime")
    s.add_argument("action", choices=["available", "add"])
    s.add_argument("kind", nargs="?")
    change_flags(s)
    s = steps.add_parser("admin", parents=[link], help="the first admin's own account: the email, their password on stdin")
    s.add_argument("email")
    s = steps.add_parser("agents", parents=[link], help="optional: the coding agents whose connections to "
                         "suggest: KIND ... or none (every template has every agent); without one, what is offered")
    s.add_argument("kinds", nargs="*", metavar="KIND")
    s.add_argument("--json", action="store_true")
    s = steps.add_parser("finish", parents=[link], help="finish: as the admin sbx login signed in, or with the "
                         "setup link and --admin, as that admin (their own password on stdin)")
    s.add_argument("--admin", metavar="EMAIL")
    s.add_argument("--json", action="store_true")
    p = command("down", down, "delete the sandbox and what was provisioned for it, after the recipe's "
                ".sbx/cleanup")
    p.add_argument("name")
    p.add_argument("--json", action="store_true", help="the server's answer, with what .sbx/cleanup did")
    p.add_argument("--timeout", type=seconds, help=f"seconds to wait for the server's answer (default "
                   f"{DOWN_TIMEOUT_SECONDS}: a paused sandbox's resume and its cleanup come first)")
    p = command("ls", ls, "your sandboxes")
    p.add_argument("--json", action="store_true")
    p = command("urls", urls_cmd, "a sandbox's preview links, and ports servers listen on")
    p.add_argument("name")
    p = command("open", lambda a: open_tool(a.name, a.tool), "open a tool in the sandbox (an agent on "
                "your own login, which goes along)")
    p.add_argument("name")
    p.add_argument("tool", help=tools_help)
    p = command("logs", logs, "follow the app's logs")
    p.add_argument("name")
    p = command("push", push, "push a local branch into the sandbox's checkout")
    p.add_argument("name")
    p.add_argument("--branch", help="default: the current one")
    p = command("sandbox", sandbox_cmd, "agent operations; each prints the sandbox's JSON on stdout (progress goes to "
                "stderr): info [--wait], restart (the app), prepare (reapply the recipe), pause, resume, resources, "
                "exec (a script on stdin; prints its output)")
    p.add_argument("action", choices=["info", "restart", "prepare", "pause", "resume", "resources", "exec"])
    p.add_argument("name")
    p.add_argument("--wait", action="store_true", help="info: until its operation finishes")
    p.add_argument("--no-app", action="store_true", help="resume: without starting the app")
    p.add_argument("--timeout", type=seconds, default=1800, help="seconds to wait (exec: to run; default 1800)")
    p = command("ssh-config", ssh_config_cmd, "make sandboxes reachable as sbx-NAME from ssh and editors "
                "(an Include in ~/.ssh/config)")
    p.add_argument("--apps", action="store_true", help="also copy the hosts for Claude Desktop and the Codex app, "
                   "and register them in Claude Desktop (again after up or down)")
    p.add_argument("--remove", action="store_true", help="take all of it out again")
    p = command("repo", repo_cmd, "status: what the server knows about the repo; connect: set it up for sandboxes "
                "(an admin adds it to the server; drafts .sbx/ from the checkout); brief: print the setup brief "
                "an agent follows")
    p.add_argument("action", choices=["status", "connect", "brief"])
    p.add_argument("--repo", help=repo_help)
    p.add_argument("--dir", help="connect: the checkout (default: the one you are in)")
    p.add_argument("--agent", nargs="?", const="claude", metavar="COMMAND",
                   help="connect: let this agent CLI (default claude; codex, ...) finish and verify the recipe")
    p.add_argument("--prebuild", action="store_true", help="connect: draft a recipe with a template of its own")
    p.add_argument("--json", action="store_true", help="status: as the API answers")
    p = command("recipe", recipe_cmd, "apply: install the checkout's .sbx/ on the server (admins); "
                "rm: remove what apply installed (admins); pr: propose it as a draft PR through the GitHub App, "
                "or update the open one (the server opens it on its own once a sandbox comes up healthy from an "
                "applied recipe)")
    p.add_argument("action", choices=["apply", "rm", "pr"])
    p.add_argument("dir", nargs="?", help="the checkout or its .sbx folder (default: the checkout you are in)")
    p.add_argument("--repo", help=repo_help)
    p.add_argument("--title", default="Set up dev sandboxes", help="pr: its title")
    p.add_argument("--yes", action="store_true", help="pr: publish without asking")
    p.add_argument("--status", action="store_true", help="pr: the recipe's PR (its link), or why sbx could not "
                   "open it (exits 1)")
    p.add_argument("--json", action="store_true", help="pr: as the API answers")
    p = command("template", template_cmd, "build the repo's template from its default branch, its build's status, "
                "or rm: delete its templates and build record (admins)")
    p.add_argument("action", choices=["build", "status", "rm"])
    p.add_argument("--repo", help=repo_help)
    p.add_argument("--wait", action="store_true", help="until the build finishes")
    p.add_argument("--timeout", type=seconds, default=1800, help="with --wait: seconds to wait for the build "
                   "(default 1800)")
    p = command("env", env_cmd, "the repo's env on the server: what its sandboxes get, where each value comes "
                "from; a change reaches running sandboxes within seconds",
                epilog="sbx env ls                 every variable, its value (not secrets') and source\n"
                       "sbx env set NAME [VALUE]   set it; without VALUE it asks (hidden) or reads stdin\n"
                       "sbx env rm NAME            remove it\n"
                       "sbx env import FILE        a .env file (- for stdin), as secrets\n"
                       "sbx env set NAME --installation   (admins) a secret the installation's\n"
                       "                           variables may name as ${secret:NAME}",
                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["ls", "set", "rm", "import"])
    p.add_argument("args", nargs="*")
    p.add_argument("--repo", help=repo_help)
    p.add_argument("--json", action="store_true")
    p.add_argument("--mine", action="store_true", help="your own value, for your sandboxes only")
    p.add_argument("--for", dest="audience", default="all", help="which sandboxes get it: all, previews or sessions")
    p.add_argument("--secret", action="store_true", help="write-only; new values are, this also makes a plain one so")
    p.add_argument("--plain", action="store_true", help="not a secret: typed visibly, shown in sbx env ls")
    p.add_argument("--hide-from-agents", action="store_true", help="only the app gets it, not agents' shells")
    p.add_argument("--no-restart", action="store_true", help="running apps read it when they next restart")
    p.add_argument("--installation", action="store_true",
                   help="admins: the installation's own secrets (ls, set, rm), not a repo's")
    p = command("catalog", catalog_cmd, "the packages: ls, show NAME, new NAME --from PKG, push DIR, test NAME, "
                "brief [--agent claude]")
    p.add_argument("action", nargs="?", choices=["ls", "show", "new", "push", "test", "brief"])
    p.add_argument("name", nargs="?")
    p.add_argument("--kind", choices=["provision", "connection"], help="ls: only this kind")
    p.add_argument("--json", action="store_true")
    p.add_argument("--files", action="store_true", help="show: print the files too")
    p.add_argument("--from", dest="from_", help="new: copy this package's files as the start")
    p.add_argument("--input", action="append", help="test: k=v, repeatable")
    p.add_argument("--connection", action="append", help="test: slot=key")
    p.add_argument("--message", help="push: a note for the history")
    p.add_argument("--what", help="brief: the service to write a package for")
    p.add_argument("--agent", metavar="COMMAND", help="brief: start this agent CLI (claude, codex, ...) with it")
    p = command("attach", attach_cmd, "plug a package into the repo's sandboxes; secret inputs are asked for (hidden)")
    p.add_argument("package")
    p.add_argument("pairs", nargs="*", metavar="name=value")
    p.add_argument("--key")
    p.add_argument("--repo", help=repo_help)
    p.add_argument("--connection", action="append", help="slot=key: the saved connection a required slot uses")
    p = command("detach", detach_cmd, "remove an attachment (disable one .sbx/sbx.yaml declares)")
    p.add_argument("key")
    p.add_argument("--repo", help=repo_help)
    command("update", update, "install the newest sbx")
    command("doctor", doctor, "check this Mac's setup")
    command("version", lambda _a: print(VERSION), "this sbx's version")
    p = command("tunnel", tunnel, "the SSH ProxyCommand sbx writes (not run by hand)")
    p.add_argument("sandbox_id")
    p.add_argument("url")
    return top


def main():
    args = parser().parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    args.func(args)


if __name__ == "__main__":
    main()
