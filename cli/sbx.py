#!/usr/bin/env python3
"""sbx: dev sandboxes on the sbx-control that installed it.

A thin client of its HTTP API: everything it shows or connects to (URLs,
the SSH bridge, readiness, where agent logins go, recipe scaffolds) comes
from the server. `sbx COMMAND -h` explains a command.
"""
import argparse
import base64
import getpass
import hashlib
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


class Client:
    """sbx-control at base, signed in with the session's API token from the
    Keychain, or with a first-run setup link's token (Authorization: Setup)
    over that link's own TLS context (its certificate may be the self-signed
    one the installer made: --insecure)."""

    def __init__(self, base, setup_token=None, context=None):
        self.base, self.setup_token, self.context = base, setup_token, context

    def _authorization(self):
        if self.setup_token:
            return f"Setup {self.setup_token}"
        token = keychain_get(API_TOKEN_SERVICE)
        if not token:
            raise Refused("not signed in: run `sbx login`")
        return f"Bearer {token}"

    def _moved(self, url):
        """The sign-in host was renamed (its old one answers 308): use the new
        one from now on, and remember it when this is the installed CLI's server."""
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if not parts.netloc or origin == self.base:
            return False
        if self.base == CONTROL:
            moved(origin)
        self.base = origin
        return True

    def request(self, method, path, body=None, raw=False, timeout=600, _retry=True):
        call = urllib.request.Request(f"{self.base}{path}", method=method,
                                      data=json.dumps(body).encode() if body is not None else None,
                                      headers={"Authorization": self._authorization(),
                                               "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(call, timeout=timeout, context=self.context) as response:
                if response.url != call.full_url:  # a GET followed the 308
                    self._moved(response.url)
                server = response.headers.get("X-Sbx-Version")
                if server and VERSION not in ("dev", server):
                    print(f"sbx {server} is available (you have {VERSION}): sbx update", file=sys.stderr)
                data = response.read()
                return data.decode() if raw else json.loads(data or b"null")
        except urllib.error.HTTPError as error:
            if error.code in (307, 308) and _retry and self._moved(error.headers.get("Location", "")):
                error.close()
                return self.request(method, path, body, raw, timeout, _retry=False)  # urllib follows only a GET
            detail = error.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            if isinstance(detail, dict):   # {message, checks}: what failed, each check that did
                detail = "; ".join([detail.get("message") or "refused", *(f"{c.get('name')}: {c.get('detail')}"
                                                                          for c in detail.get("checks") or [] if not c.get("ok"))])
            if error.code == 401:
                raise Refused("the setup link was refused: it is used up or replaced (sbx-admin setup-link prints a "
                              "new one)" if self.setup_token else "session expired: run `sbx login`", 401) from None
            raise Refused(f"sbx-control: {detail}", error.code) from None

    def api(self, method, path, body=None, raw=False, timeout=600):
        """request(), exiting with the refusal or why the server could not be reached (request() itself
        raises OSError, which follow() retries while sbx-control restarts)."""
        try:
            return self.request(method, path, body, raw, timeout)
        except Refused as error:
            sys.exit(str(error))
        except OSError as error:
            reason = getattr(error, "reason", None) or error
            hint = "; add --insecure for a self-signed certificate" if self.setup_token and self.context is None else ""
            sys.exit(f"cannot reach {self.base}: {reason}{hint}")


def moved(url):
    """sbx-control is at url's origin now: the installed CLI's server from now on."""
    global CONTROL
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if not parts.netloc or origin == CONTROL:
        return False
    CONTROL = origin
    if not os.environ.get("SBX_CONTROL"):
        INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
        (INSTALL_ROOT / "control").write_text(origin + "\n")
    note(f"sbx-control moved to {origin}")
    return True


def request(method, path, body=None, raw=False, timeout=600):
    return Client(CONTROL).request(method, path, body, raw, timeout)


def api(method, path, body=None, raw=False, timeout=600):
    return Client(CONTROL).api(method, path, body, raw, timeout)


def note(text):
    print(text, file=sys.stderr)


RETRIED = (502, 503, 504)   # sbx-control restarting behind nginx


def follow(fetch, verdict, timeout, *, interval=2.0, first=None, retry_statuses=(), late="still waiting"):
    """What fetch() answers, followed until verdict(found) -> (progress, done)
    says it is done: then that answer. Progress (text or None) is noted when
    it changes; done is True, False, or why it failed (a text: it exits with
    it). first: an answer in hand already. A fetch refused with one of retry_statuses, or a dropped
    connection while there are any, is tried again; any other refusal exits.
    Past timeout: exits with late (or late(found))."""
    deadline, said = time.monotonic() + timeout, None
    found = fetch() if first is None else first
    while True:
        progress, done = verdict(found)
        if progress and progress != said:
            note(progress)
            said = progress
        if isinstance(done, str):
            sys.exit(done)
        if done:
            return found
        if time.monotonic() > deadline:
            sys.exit(late(found) if callable(late) else late)
        if interval:
            time.sleep(interval)
        try:
            found = fetch()
        except Refused as error:
            if error.status not in retry_statuses:
                sys.exit(str(error))
        except OSError:   # refused or dropped connections while it restarts
            if not retry_statuses:
                raise


def login_control():
    """Browser sign-in: sbx-control hands a loopback listener a one-time
    code, which is exchanged here, with the verifier only this process
    knows, for the API token."""
    state, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(48)
    try:
        with urllib.request.urlopen(f"{CONTROL}/dist/version", timeout=30) as response:
            moved(response.url)  # sign in where sbx-control is now, not through a redirect
    except (urllib.error.URLError, OSError):
        pass
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
    deadline = time.time() + 300
    while "code" not in received and time.time() < deadline:
        server.timeout = 1
        server.handle_request()
    server.server_close()
    if "code" not in received:
        sys.exit("sign-in timed out")
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
        login_control()
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


def rewrite_ssh_config(block):
    text = SSH_CONFIG.read_text() if SSH_CONFIG.exists() else ""
    begin, end = SSH_BLOCK
    if begin in text and end in text:
        text = text[:text.index(begin)] + text[text.index(end) + len(end):].lstrip("\n")
    SSH_CONFIG.parent.mkdir(mode=0o700, exist_ok=True)
    SSH_CONFIG.write_text(block + text)
    SSH_CONFIG.chmod(0o600)


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
    lines = [begin, f"Include \"{SSH_DIR}/*.conf\""]
    if args.apps:
        # Claude Desktop and the Codex app list hosts from this file and may
        # not follow Include: copies, current until the next up or down.
        lines += [conf.read_text().rstrip("\n") for conf in confs]
    rewrite_ssh_config("\n".join(lines) + f"\n{end}\n\n")
    print(f"{SSH_CONFIG} includes {SSH_DIR}: ssh, VS Code, Cursor and Zed reach sbx-NAME")
    if args.apps:
        names = [conf.stem for conf in confs]
        registered = claude_desktop_hosts(names)
        print(f"copied {len(names)} host(s) for GUI apps{' and registered them in Claude Desktop' if registered else ''}; "
              "run `sbx ssh-config --apps` again after up or down")


def websocat():
    return shutil.which("websocat", path=f"/opt/homebrew/bin:/usr/local/bin:{os.environ.get('PATH', '')}")


def tunnel(args):
    """SSH ProxyCommand: the sandbox's sshd over a WebSocket through the
    gate in front of the sandboxes, which checks this token and that you own
    the sandbox."""
    token = keychain_get(API_TOKEN_SERVICE)
    if not token:
        sys.exit("not signed in: run `sbx login`")
    binary = websocat() or sys.exit("websocat not found: brew install websocat")
    os.execv(binary, [binary, "--binary", "-B", "65536", f"--header=Authorization: Bearer {token}", "-", args.url])


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
    return follow(lambda: api("GET", f"/api/sandboxes/{name}?wait=30", timeout=90), verdict, timeout, interval=0,
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
    info = api("POST", "/api/sandboxes", body)
    if write_host(args.name, info) and keys:
        hand_credentials(args.name)
    note(f"[{time.time() - started:4.1f}s] sbx-{args.name} is on {info.get('branch') or branch or 'the default branch'}")
    if not args.no_wait:
        info = wait_for(args.name, args.timeout)
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


def open_tool(name, tool):
    info = api("GET", f"/api/sandboxes/{name}")
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
    if not found["ok"]:
        sys.exit(1)


def _read_secret(prompt):
    """One line of stdin (hidden at a terminal), never argv or the environment."""
    value = getpass.getpass(f"{prompt} (hidden): ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    return value.strip()


def _runtime_body(args, runtime, adding):
    """What a change sends: --plan (the first plan when adding without one; the server resolves its limits),
    each --set, and each --secret read from stdin (one line each, in order; hidden at a terminal): a secret
    never on the command line."""
    settings = {s["key"]: s for s in runtime.get("settings") or []}
    presets = runtime.get("presets") or []
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
    plan = args.plan or (presets[0]["name"] if adding and presets else None)
    return {"plan": plan, "values": values} if plan else {"values": values}


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


# sbx setup: a fresh installation's first-run setup, without a browser, through the setup link


def setup_client(args):
    """sbx-control as the setup link names it, from SBX_SETUP_LINK or --link-file (never argv: it is a
    credential, and stdin is the secrets'), signed in with its token."""
    if args.link_file:
        link = pathlib.Path(args.link_file).expanduser().read_text().strip()
    else:
        link = os.environ.get("SBX_SETUP_LINK", "").strip()
    if not link:
        sys.exit("the setup link install.sh printed (or sbx-admin setup-link): in SBX_SETUP_LINK, or a file named "
                 "by --link-file; never on the command line")
    url = urllib.parse.urlsplit(link)
    token = urllib.parse.parse_qs(url.fragment).get("t", [""])[0]
    # http only to this machine itself (a tunnel): the token never crosses a network in plain text.
    plain_ok = url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
    if not (url.scheme == "https" or plain_ok) or not url.netloc or not token:
        sys.exit("that is not a setup link: https://HOST/setup#t=TOKEN")
    context = None
    if args.insecure:
        import ssl
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return Client(f"{url.scheme}://{url.netloc}", token, context)


def setup_cmd(args):
    client = setup_client(args)
    {"status": setup_status, "address": setup_address, "runtime": setup_runtime, "admin": setup_admin,
     "finish": setup_finish}[args.step](args, client)


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
    names = {"address": "address", "runtime": "runtime", "sign_in": "admin"}
    for step in ("address", "runtime", "sign_in"):
        found = _steps(state).get(step)
        if found:
            print(f"{'done' if found['done'] else 'todo':<5} {names[step]:<8} {found['summary']}")
    print(f"server address: {state['host'].get('address') or 'unknown'}; link: {state.get('link', '')}")
    print("ready to finish: sbx setup finish" if state.get("can_complete") else
          "next: sbx setup address | runtime add | admin, then sbx setup finish")


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
        return f"waiting for the certificate{': ' + error if error else ''}", False
    state = follow(lambda: client.api("GET", "/api/setup"), verdict, args.timeout, interval=5,
                   late=lambda state: f"HTTPS is not ready after {args.timeout} s "
                                      f"({state['access'].get('certificate_error') or 'no certificate yet'}); "
                                      "sbx setup status")
    print(f"address ready: {state['access']['url']}" + (f"; the setup link is now {state['link']}" if args.domain else ""))


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
    print(f"{args.email} has an account and is an admin; next: sbx setup finish --admin {args.email} "
          "(their password on stdin again)")


def setup_finish(args, client):
    """Finish the setup; with --admin, as that admin (their own password on stdin)."""
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
    print(f"set up: {state['access']['url']}; the setup link no longer works. Next: sbx login")


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


def down(args):
    api("DELETE", f"/api/sandboxes/{args.name}")
    forget_host(args.name)
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
    files, executable = recipe_files(default_folder(args.dir))
    if args.action == "apply":
        r = api("PUT", f"/api/repos/{repo}/recipe", {"files": files, "executable": executable})
        print(f"{repo}: {r['files']} recipe file(s) installed on the server"
              + ("; it has a template of its own: sbx template build" if r["prebuilt"] else ""))
        return
    print(f"Open a draft setup PR on {repo} through the server's GitHub App:")
    for path in files:
        print(f"  {path}")
    print("Review these files first. Credentials belong on the sandbox server, not in the PR.")
    if input("Publish this proposal? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    print(api("POST", f"/api/repos/{repo}/setup-pr", {"files": files, "title": args.title})["url"])


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
        for key, value in api("GET", f"/api/repos/{repo}").items():
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
                   "without one is on the first plan (hobby)")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="add, set: a setting, in its unit (seconds, MiB); repeatable; a plan fixes its own limits")
    p.add_argument("--secret", action="append", metavar="KEY",
                   help="add, set: a secret setting, its value read from stdin (one line each), never argv")
    p.add_argument("--no-wait", action="store_true", help="return once the change is queued")
    p.add_argument("--timeout", type=int, default=600, help="seconds to wait for the change (default 600)")
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
    p = command("up", up, "a sandbox of the repo you are in, on your branch if GitHub has it; waits until it is ready")
    p.add_argument("name")
    p.add_argument("--repo", help=repo_help)
    p.add_argument("--branch", help="default: your current branch if pushed, else the repo's default")
    p.add_argument("--open", metavar="TOOL", help=f"then open this tool in it: {tools_help}")
    p.add_argument("--json", action="store_true", help="print the sandbox as the API returns it")
    p.add_argument("--no-wait", action="store_true", help="return once SSH works")
    p.add_argument("--timeout", type=int, default=1800, help="seconds to wait for it (default 1800)")
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
    p.add_argument("--timeout", type=int, default=300, help="seconds to wait for the server to apply it (default 300)")
    p.add_argument("--json", action="store_true", help="as the API answers")

    p = command("setup", setup_cmd, "a fresh server's first-run setup without a browser, through the link install.sh "
                "printed (in SBX_SETUP_LINK or a --link-file, never argv); secrets on stdin")
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
    s.add_argument("--domain", metavar="D", help="previews at <name>.D, the server at login.D")
    s.add_argument("--email", help="for the certificate's expiry notices (with --domain)")
    s.add_argument("--dns", default="delegated", choices=["delegated", "cloudflare", "route53", "none"],
                   help="how the wildcard certificate is issued (default delegated); none: self-signed, for a first "
                        "test (browsers warn, and only sbx setup takes it, with --insecure)")
    s.add_argument("--secret", action="append", metavar="KEY",
                   help="the DNS provider's credential (cloudflare_api_token, aws_access_key_id, ...) on stdin")
    s.add_argument("--no-wait", action="store_true")
    s.add_argument("--timeout", type=int, default=900)
    s = steps.add_parser("runtime", parents=[link], help="what can run here, and add the first sandbox runtime")
    s.add_argument("action", choices=["available", "add"])
    s.add_argument("kind", nargs="?")
    change_flags(s)
    s = steps.add_parser("admin", parents=[link], help="the first admin's own account: the email, their password on stdin")
    s.add_argument("email")
    s = steps.add_parser("finish", parents=[link], help="finish; with --admin, as that admin (their own password on stdin)")
    s.add_argument("--admin", metavar="EMAIL")
    s.add_argument("--json", action="store_true")
    p = command("down", down, "delete the sandbox and what was provisioned for it")
    p.add_argument("name")
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
    p.add_argument("--timeout", type=int, default=1800, help="seconds to wait (exec: to run; default 1800)")
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
    p = command("recipe", recipe_cmd, "apply: install the checkout's .sbx/ on the server (admins); "
                "rm: remove what apply installed (admins); pr: propose it as a draft PR through the GitHub App")
    p.add_argument("action", choices=["apply", "rm", "pr"])
    p.add_argument("dir", nargs="?", help="the checkout or its .sbx folder (default: the checkout you are in)")
    p.add_argument("--repo", help=repo_help)
    p.add_argument("--title", default="Set up dev sandboxes", help="pr: its title")
    p = command("template", template_cmd, "build the repo's template from its default branch, its build's status, "
                "or rm: delete its templates and build record (admins)")
    p.add_argument("action", choices=["build", "status", "rm"])
    p.add_argument("--repo", help=repo_help)
    p.add_argument("--wait", action="store_true", help="until the build finishes")
    p.add_argument("--timeout", type=int, default=1800)
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
