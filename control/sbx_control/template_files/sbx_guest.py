#!/usr/bin/env python3
"""The platform's helper inside a sandbox. sbx-control ships it into the
sandbox's runtime directory (core.ship_guest) and runs one subcommand:

  processes restart|stop|probe|supervise   the recipe's named processes
  identity WORKSPACE_ID                    fresh SSH host keys, once per workspace (root)
  swap --mb N                              the recipe's swap budget (root)
  usage                                    memory and disk use, as JSON
  forward ensure|run CONFIG                the endpoints' forwarder (127.0.0.1 to TLS)

The functions above main() are also what extensions' own guest scripts
import (they are shipped next to this file). Standard library only: it runs
on whatever Python the template has.

Processes are owned through an environment marker (VARIABLE=value in
/proc/<pid>/environ), and a pid stands for the same process only while its
start time (/proc/<pid>/stat field 22) is unchanged."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time

RUN = Path(__file__).resolve().parent   # the runtime directory it was shipped into
PROC = Path("/proc")
SCOPE = "_SBX_PROCESS_SCOPE"            # marks every process a supervised process started
START_SCOPE = "_SBX_APP_START_SCOPE"    # marks what .sbx/start launched (core.start_app)
START_SCOPE_FILE = "app-start-scope.json"   # in RUN: the marker value of the last .sbx/start
SWAPFILE = Path("/swapfile")            # also what the template's sbx-swap.service creates (resource-control.sh)
# Where the app's output goes: .sbx/setup's, the supervisor's, each process's.
SETUP_LOG = "/tmp/sbx-setup.log"
SUPERVISOR_LOG = "/tmp/sbx-supervisor.log"


def log_path(process: str) -> str:
    return f"/tmp/sbx-{process}.log"


class GuestError(Exception):
    """A refusal the caller sees as the command's error."""


def start_ticks(pid, proc=PROC):
    """The process's start time in clock ticks, or None when it is gone or a
    zombie. comm (field 2) may itself contain spaces and parentheses."""
    try:
        fields = (Path(proc) / str(pid) / "stat").read_text().rpartition(") ")[2].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def record(pid, proc=PROC):
    return {"pid": pid, "birth": start_ticks(pid, proc)}


def alive(entry, proc=PROC):
    return bool(entry and entry.get("birth") and start_ticks(entry["pid"], proc) == entry["birth"])


def marked(variable, value, proc=PROC):
    if not value:
        return []
    marker = f"{variable}={value}".encode()
    found = []
    for path in Path(proc).iterdir():
        if not path.name.isdecimal():
            continue
        try:
            if marker in (path / "environ").read_bytes().split(b"\0"):
                entry = record(int(path.name), proc)
                if entry["birth"]:
                    found.append(entry)
        except OSError:
            pass
    return found


def send(pid, sig, group=False):
    try:
        os.kill(-pid if group else pid, sig)
    except ProcessLookupError:
        pass


def wait_until(done, timeout, interval=0.1):
    """Poll done() until it is true or timeout seconds passed; its last answer."""
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


def drain(signal_all, remaining, grace=10.0, kill_grace=5.0, interval=0.1):
    """SIGTERM, up to grace seconds for remaining() to empty, then SIGKILL
    and up to kill_grace more. True when nothing remains."""
    signal_all(signal.SIGTERM)
    if wait_until(lambda: not remaining(), grace, interval):
        return True
    signal_all(signal.SIGKILL)
    return wait_until(lambda: not remaining(), kill_grace, interval)


def terminate(pid, still_owned, grace=3.0):
    """Stop one process through a pidfd, which a reused pid cannot redirect:
    still_owned() is checked again once the handle is held. False when it
    outlived SIGKILL by another grace period too."""
    try:
        handle = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    try:
        if not still_owned():
            return True
        signal.pidfd_send_signal(handle, signal.SIGTERM)
        if not select.select([handle], [], [], grace)[0]:   # readable once it exited
            signal.pidfd_send_signal(handle, signal.SIGKILL)
            return bool(select.select([handle], [], [], grace)[0])
    except ProcessLookupError:
        pass
    finally:
        os.close(handle)
    return True


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def say(message):
    """A line in the supervisor's log, which a full disk may refuse too."""
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass


def write_json(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# processes: a small supervisor for the recipe's named processes, no systemd needed.

NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}")
HEALTHY_AFTER = 30   # seconds running that clear a process's failures
MAX_FAILURES = 5
BACKOFF = 2


def supervise(root, workdir, env_file):
    stopping = False

    def shutdown(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    children, processes = {}, {}
    manager = record(os.getpid())
    spec = read_json(root / "spec.json")
    unwritten = False
    try:
        while not stopping:
            now = time.monotonic()
            for name, command in spec.items():
                previous = processes.get(name, {})
                child = children.get(name)
                if child and child.poll() is None:
                    if now - previous["started"] >= HEALTHY_AFTER:
                        previous["failures"] = 0
                    continue
                if child:
                    # A launcher can exit while leaving a worker in its group or session.
                    for entry in marked(SCOPE, previous.get("scope")):
                        send(entry["pid"], signal.SIGKILL)
                    send(child.pid, signal.SIGKILL, group=True)
                    previous.update(status="backoff" if child.returncode else "exited",
                                    exit_code=child.returncode, failures=previous["failures"] + 1,
                                    retry_at=now + BACKOFF)
                    children.pop(name)
                if previous.get("status") == "exited" or previous.get("failures", 0) >= MAX_FAILURES:
                    previous["status"] = "exited"
                    continue
                if previous.get("retry_at", 0) > now:
                    continue
                scope = secrets.token_hex(16)
                with open(log_path(name), "ab", buffering=0) as log:
                    # bash reads the applied environment, multiline values included.
                    child = subprocess.Popen(["/bin/bash", "-c", f"test -r {shlex.quote(env_file)} || exit 1; "
                                                                 f". {shlex.quote(env_file)}; {command}"],
                                             cwd=workdir, stdin=subprocess.DEVNULL, stdout=log,
                                             stderr=subprocess.STDOUT, start_new_session=True,
                                             env={**os.environ, SCOPE: scope})
                children[name] = child
                processes[name] = {**record(child.pid), "status": "running", "started": now,
                                   "failures": previous.get("failures", 0), "scope": scope}
            try:
                write_json(root / "state.json", {**manager, "processes": processes})
                unwritten = False
            except OSError as error:
                # A full tmpfs (an agent's files filled /run) must not stop the
                # app: keep supervising with the last state written, say so once.
                if not unwritten:
                    say(f"sbx-guest supervise: could not write {root / 'state.json'} ({error}); "
                        "the processes keep running")
                unwritten = True
            time.sleep(0.2)
    finally:
        def signal_all(sig):
            for entry in processes.values():
                for owned in marked(SCOPE, entry.get("scope")):
                    send(owned["pid"], sig)
            for child in children.values():
                send(child.pid, sig, group=True)
        drain(signal_all, lambda: (any(child.poll() is None for child in children.values()) or
                                   any(marked(SCOPE, p.get("scope")) for p in processes.values())),
              grace=10, kill_grace=0)
        for child in children.values():
            child.wait()


def stop_all(root):
    """Stop the supervisor, then everything its processes and the last
    .sbx/start launched (they may have left their process groups)."""
    previous = read_json(root / "state.json")
    if alive(previous):
        send(previous["pid"], signal.SIGTERM)
        if not wait_until(lambda: not alive(previous), 12):
            raise GuestError("the previous app supervisor did not stop; refusing to start duplicates")
    started = read_json(root.parent / START_SCOPE_FILE).get("scope")
    owned = previous.get("processes", {}).values()
    scopes = [(p.get("scope"), SCOPE) for p in owned] + ([(started, START_SCOPE)] if started else [])

    def signal_all(sig):
        for value, variable in scopes:
            for entry in marked(variable, value):
                send(entry["pid"], sig)
        for entry in owned:
            if alive(entry):
                send(entry["pid"], sig, group=True)

    def remaining():
        return any(alive(p) for p in owned) or any(marked(variable, value) for value, variable in scopes)

    if not drain(signal_all, remaining, grace=10, kill_grace=5, interval=0.05):
        raise GuestError("owned app process drain outcome unknown")
    (root.parent / START_SCOPE_FILE).unlink(missing_ok=True)
    (root / "state.json").unlink(missing_ok=True)


def restart(root, workdir, env_file, spec):
    if not isinstance(spec, dict) or any(not NAME.fullmatch(name) or not isinstance(command, str)
                                        or not command.strip() for name, command in spec.items()):
        raise GuestError("processes must map simple names to nonempty commands")
    # One request at a time, so none can leave an untracked supervisor.
    with (root / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stop_all(root)
        if not spec:
            return
        write_json(root / "spec.json", spec)
        with open(SUPERVISOR_LOG, "ab", buffering=0) as log:
            manager = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "processes", "supervise",
                                        "--root", str(root), "--workdir", workdir, "--env-file", env_file],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True,
                                       env={k: v for k, v in os.environ.items() if k != SCOPE})

        def ready():
            state = read_json(root / "state.json")
            return state.get("pid") == manager.pid and alive(state)
        if not wait_until(lambda: ready() or manager.poll() is not None, 5) or not ready():
            raise GuestError(f"app supervisor failed to start; see {SUPERVISOR_LOG}")


def probe(root):
    state = read_json(root / "state.json")
    healthy = alive(state) and all(p["status"] != "exited" for p in state.get("processes", {}).values())
    return "alive" if healthy else "exited"


def processes(args):
    root = Path(args.root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == "supervise":
        supervise(root, args.workdir, args.env_file)
    elif args.action == "probe":
        print(probe(root))
    elif args.action == "stop":
        restart(root, args.workdir, args.env_file, {})
    else:
        restart(root, args.workdir, args.env_file, json.loads(args.spec))


# identity: a workspace's own SSH host keys, replacing what its template was built with.

HOST_KEYS = ("ed25519", "ecdsa", "rsa")
SSH = Path("/etc/ssh")
IDENTITY = Path("/etc/sbx-workspace-identity.json")


def key_permissions(directory):
    for kind in HOST_KEYS:
        (directory / f"ssh_host_{kind}_key").chmod(0o600)
        (directory / f"ssh_host_{kind}_key.pub").chmod(0o644)


def listener_pid():
    """sshd's pid: systemd's ssh.service, or the standalone sshd START runs
    where there is no systemd."""
    pid = 0
    if Path("/proc/1/comm").read_text().strip() == "systemd":
        service = subprocess.run(["systemctl", "show", "ssh.service", "--property=MainPID", "--value"],
                                 capture_output=True, text=True, check=True, timeout=10)
        pid = int(service.stdout.strip())
    if pid <= 1:
        pid = int(Path("/run/sshd.pid").read_text().strip())
    if pid <= 1:
        raise GuestError("workspace SSH listener is not running")
    return pid


def identity(workspace, ssh_port):
    if os.geteuid() != 0 or not re.fullmatch(r"[a-f0-9]{32}", workspace):
        raise GuestError("fresh workspace SSH identity requires an owned logical identity")
    os.umask(0o077)
    public = SSH / "ssh_host_ed25519_key.pub"
    with open("/run/sbx-ssh-identity.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read_json(IDENTITY)
        if previous.get("workspace_id") == workspace and previous.get("phase") == "ready":
            if public.read_text().split()[:2] != previous.get("ed25519_public"):
                raise GuestError("workspace SSH identity changed after initialization")
            key_permissions(SSH)
            return
        sessions = subprocess.run(["ss", "-Htn", "state", "established", f"( sport = :{ssh_port} )"],
                                  capture_output=True, text=True, check=True, timeout=10)
        if sessions.stdout.strip():
            raise GuestError("fresh workspace has an unexpected SSH session")
        pid = listener_pid()
        handle = os.pidfd_open(pid)
        try:
            if str((PROC / str(pid) / "exe").resolve()) != "/usr/sbin/sshd":
                raise GuestError("workspace SSH listener identity changed")
            ticks = start_ticks(pid)
            write_json(IDENTITY, {"workspace_id": workspace, "phase": "rotating"})
            with tempfile.TemporaryDirectory(prefix=".sbx-hostkeys-", dir=SSH) as temporary:
                temporary = Path(temporary)
                for kind in HOST_KEYS:
                    subprocess.run(["ssh-keygen", "-q", "-t", kind, "-N", "", "-f", str(temporary / f"ssh_host_{kind}_key")],
                                   check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=30)
                key_permissions(temporary)
                for path in temporary.iterdir():
                    os.replace(path, SSH / path.name)
            if start_ticks(pid) != ticks:
                raise GuestError("workspace SSH listener changed during initialization")
            signal.pidfd_send_signal(handle, signal.SIGHUP)
        finally:
            os.close(handle)
        expected = public.read_text().split()[:2]

        def adopted():
            scanned = subprocess.run(["ssh-keyscan", "-T", "1", "-t", "ed25519", "127.0.0.1"],
                                     capture_output=True, text=True, timeout=3)
            return any(line.split()[1:3] == expected for line in scanned.stdout.splitlines() if not line.startswith("#"))
        if not wait_until(adopted, 15):
            raise GuestError("workspace SSH listener did not adopt its fresh identity")
        write_json(IDENTITY, {"workspace_id": workspace, "phase": "ready", "ed25519_public": expected})


# swap: resize the swap file without changing the VM's allocation.

def swap(megabytes, path=SWAPFILE):
    if megabytes <= 0:
        raise GuestError("the swap budget must be positive")
    target = megabytes * 1024**2
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
        raise GuestError("refusing to replace a nonregular swapfile")
    active = {}
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        fields = line.split()
        active[fields[0]] = int(fields[3]) * 1024
    if path.exists() and str(path) not in active:
        with path.open("rb") as source:
            source.seek(4096 - 10)
            if source.read(10) != b"SWAPSPACE2":
                raise GuestError("refusing to replace an unrecognized inactive swapfile")
    if path.exists() and path.stat().st_size == target and str(path) in active:
        return
    if active.get(str(path), 0) + 256 * 1024**2 > memory()["MemAvailable"]:
        raise GuestError("not enough available RAM to change active swap safely; use a fresh workspace")
    if str(path) in active:
        subprocess.run(["swapoff", str(path)], check=True)
    previous = path.stat().st_size if path.exists() else None

    def make(size):
        subprocess.run(["fallocate", "-l", str(size), str(path)], check=True)
        with path.open("r+b") as stream:
            stream.truncate(size)
        path.chmod(0o600)
        subprocess.run(["mkswap", "-q", str(path)], check=True)
        subprocess.run(["swapon", str(path)], check=True)
    try:
        make(target)
    except Exception:
        if previous:
            make(previous)
        raise


# usage: what the VM uses now (resources.snapshot).

def memory():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        values[key] = int(value.split()[0]) * 1024
    return values


def usage():
    m, disk = memory(), os.statvfs("/")
    return {"memory": {"total_bytes": m["MemTotal"], "available_bytes": m["MemAvailable"],
                       "swap_total_bytes": m["SwapTotal"], "swap_used_bytes": m["SwapTotal"] - m["SwapFree"]},
            "disk": {"total_bytes": disk.f_blocks * disk.f_frsize, "available_bytes": disk.f_bavail * disk.f_frsize,
                     "used_bytes": (disk.f_blocks - disk.f_bfree) * disk.f_frsize}}


# forward: the endpoints' forwarder (sbx_control/endpoints.py). It listens on
# 127.0.0.1:<local_port> for each endpoint in its config and wraps each
# connection into TLS to <address>:<port>, with the sandbox's certificate for
# that endpoint, the server verified as <address> (against the public CAs, or
# the installation's self-signed certificate when the config names one).
# Each new connection takes the newest certificate it has: the config's, or
# one sbx pushed or the forwarder pulled since (RENEWED); with less than
# RENEW_BEFORE left it first asks sbx-control for new ones, with the
# sandbox's own credential. Open connections keep the certificate they began
# with. It reads the config again every few seconds (endpoints come and go)
# and exits once the config has been gone for a while.

FORWARD_PID = "forwarder.json"          # in RUN: its pid and start time
FORWARD_POLL = 2.0
FORWARD_GONE = 15.0                     # seconds without a config before it exits
RENEWED = "endpoint-certs"              # in RUN: <name>.pem, <name>-key.pem and renewed.json {name: not_after}
RENEW_BEFORE = 3600                     # seconds left on a certificate that make a connection renew it first


def _certificate(endpoint, folder, renewed):
    """(cert path, key path, not_after) of the newest certificate for the endpoint."""
    found = [(str(folder / endpoint["cert"]), str(folder / endpoint["key"]), float(endpoint.get("not_after") or 0))]
    later = read_json(Path(renewed) / "renewed.json").get(endpoint["name"])
    if later:
        found.append((str(Path(renewed) / f"{endpoint['name']}.pem"),
                      str(Path(renewed) / f"{endpoint['name']}-key.pem"), float(later)))
    return max(found, key=lambda f: f[2])


def renew(config, folder, renewed):
    """New certificates for every endpoint the sandbox may use, from
    sbx-control (its callback, with the sandbox's credential), into renewed."""
    import ssl
    import urllib.error
    import urllib.request
    target = config["renew"]
    trust = config.get("trust")
    context = ssl.create_default_context(cafile=str(folder / trust) if trust else None)
    request = urllib.request.Request(target["url"], method="POST", data=b"{}", headers={
        "Authorization": f"Bearer {target['sandbox']}:{target['credential']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            found = json.loads(response.read())["endpoints"]
    except urllib.error.HTTPError as error:   # refused: a deleted sandbox, a revoked grant
        error.close()
        raise
    renewed = Path(renewed)
    renewed.mkdir(mode=0o700, parents=True, exist_ok=True)
    times = {}
    for item in found:
        for suffix, value in ((".pem", item["cert"]), ("-key.pem", item["key"])):
            path = renewed / f"{item['name']}{suffix}"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(value)
        times[item["name"]] = item["not_after"]
    write_json(renewed / "renewed.json", times)
    return times


def forward_context(config_path, endpoint, renewed, lock=None, now=None):
    """The TLS client context for a new connection to the endpoint, with its
    newest certificate, renewed first when it runs out within RENEW_BEFORE
    (a renewal that fails leaves the one there is, while it lasts)."""
    import contextlib
    import ssl
    config = json.loads(Path(config_path).read_text())
    folder = Path(config_path).parent
    now = time.time() if now is None else now
    with lock or contextlib.nullcontext():
        cert, key, not_after = _certificate(endpoint, folder, renewed)
        if not_after - now < RENEW_BEFORE and config.get("renew"):
            try:
                renew(config, folder, renewed)
                cert, key, not_after = _certificate(endpoint, folder, renewed)
            except (OSError, ValueError, KeyError) as error:
                say(f"forward {endpoint.get('name')}: renewing its certificate: {type(error).__name__}: {error}")
    trust = config.get("trust")
    context = ssl.create_default_context(cafile=str(folder / trust) if trust else None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


def forward_dial(address, port, timeout=10):
    import socket
    return socket.create_connection((address, port), timeout=timeout)


def _pipe(source, sink):
    import socket
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            sink.sendall(data)
    except OSError:
        pass
    finally:
        for sock, how in ((sink, socket.SHUT_WR), (source, socket.SHUT_RD)):
            try:
                sock.shutdown(how)
            except OSError:
                pass


def forward_one(client, endpoint, config_path, renewed, lock=None):
    """One app connection, through TLS to the endpoint, until both sides close."""
    import threading
    try:
        context = forward_context(config_path, endpoint, renewed, lock)
        raw = forward_dial(endpoint["address"], endpoint["port"])
        server = context.wrap_socket(raw, server_hostname=endpoint["address"])
        server.settimeout(None)
    except (OSError, ValueError) as error:
        say(f"forward {endpoint.get('name')}: {type(error).__name__}: {error}")
        client.close()
        return
    back = threading.Thread(target=_pipe, args=(server, client), daemon=True)
    back.start()
    _pipe(client, server)
    back.join()
    for sock in (client, server):
        try:
            sock.close()
        except OSError:
            pass


class Forwarder:
    """The listeners, kept to what the config says (reconcile)."""

    def __init__(self, config_path, host="127.0.0.1", renewed=None):
        import threading
        self.config_path, self.host = str(config_path), host
        self.renewed = Path(renewed) if renewed else RUN / RENEWED
        self.lock = threading.Lock()   # one renewal at a time
        self.listeners = {}   # local_port -> (socket, endpoint)

    def reconcile(self):
        """Listen for each endpoint in the config, stop for those gone;
        whether the config is there."""
        import socket
        import threading
        try:
            wanted = {int(e["local_port"]): e for e in json.loads(Path(self.config_path).read_text())["endpoints"]}
        except (OSError, ValueError, KeyError, TypeError):
            return False
        for port in [p for p in self.listeners if p not in wanted]:
            self.listeners.pop(port)[0].close()
        for port, endpoint in wanted.items():
            if port in self.listeners:
                self.listeners[port] = (self.listeners[port][0], endpoint)
                continue
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((self.host, port))
            except OSError as error:
                say(f"forward {endpoint.get('name')}: can't listen on {self.host}:{port}: {error}")
                listener.close()
                continue
            listener.listen(64)
            self.listeners[port] = (listener, endpoint)
            threading.Thread(target=self._accept, args=(port, listener), daemon=True).start()
        return True

    def _accept(self, port, listener):
        import threading
        while True:
            try:
                client, _ = listener.accept()
            except OSError:   # closed: the endpoint is gone
                return
            found = self.listeners.get(port)
            if not found or found[0] is not listener:
                client.close()
                return
            threading.Thread(target=forward_one, args=(client, found[1], self.config_path, self.renewed, self.lock),
                             daemon=True).start()

    def close(self):
        for listener, _ in self.listeners.values():
            listener.close()
        self.listeners.clear()

    def run(self, poll=FORWARD_POLL, gone=FORWARD_GONE):
        missing_since = None
        while True:
            if self.reconcile():
                missing_since = None
            else:
                missing_since = missing_since or time.monotonic()
                if time.monotonic() - missing_since > gone:
                    self.close()
                    return
            time.sleep(poll)


def forward(action, config_path):
    """ensure: start the forwarder unless it runs (detached); run: be it."""
    state_file = RUN / FORWARD_PID
    if action == "run":
        write_json(state_file, record(os.getpid()))
        Forwarder(config_path).run()
        return
    if alive(read_json(state_file)):
        return
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "forward", "run", str(config_path)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=open("/tmp/sbx-endpoints.log", "a"), start_new_session=True)
    write_json(state_file, record(process.pid))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("processes")
    p.add_argument("action", choices=["restart", "probe", "stop", "supervise"])
    p.add_argument("--root", default=str(RUN / "processes"))
    p.add_argument("--workdir", default=os.path.expanduser("~"))
    p.add_argument("--env-file", default=str(RUN / "app-env"))
    p.add_argument("--spec", default="{}")
    p = sub.add_parser("identity")
    p.add_argument("workspace")
    p.add_argument("--ssh-port", type=int, required=True)
    sub.add_parser("swap").add_argument("--mb", type=int, required=True)
    sub.add_parser("usage")
    p = sub.add_parser("forward")
    p.add_argument("action", choices=["ensure", "run"])
    p.add_argument("config")
    args = parser.parse_args(argv)
    try:
        if args.command == "processes":
            processes(args)
        elif args.command == "identity":
            identity(args.workspace, args.ssh_port)
        elif args.command == "swap":
            swap(args.mb)
        elif args.command == "forward":
            forward(args.action, args.config)
        else:
            print(json.dumps(usage()))
    except GuestError as error:
        print(f"sbx-guest {args.command}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
