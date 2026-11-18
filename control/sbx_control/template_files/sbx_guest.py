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
import shutil
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
IDENTITY_LOCK = Path("/run/sbx-ssh-identity.lock")
KEPT_KEYS = Path("/etc/sbx-host-keys")   # root's copy: a runtime whose cold boot makes new host keys gets these back


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


def restart_listener():
    """Start sshd again: systemd's ssh.service, or the standalone one."""
    if Path("/proc/1/comm").read_text().strip() == "systemd":
        subprocess.run(["systemctl", "restart", "ssh.service"], check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            old = int(Path("/run/sshd.pid").read_text().strip())
        except (OSError, ValueError):   # none recorded (stopped, or a machine just booted): only start it
            old = 0
        handle = None
        try:   # a pidfd, and sshd's binary behind it: a pid reused by another process is left alone
            handle = os.pidfd_open(old) if old > 1 else None
            if handle is not None and str((PROC / str(old) / "exe").resolve()) not in ("/usr/sbin/sshd", "/usr/sbin/sshd (deleted)"):
                os.close(handle)
                handle = None
        except OSError:
            handle = None
        if handle is not None:
            try:
                signal.pidfd_send_signal(handle, signal.SIGTERM)
            except ProcessLookupError:
                pass
            finally:
                os.close(handle)
            if not wait_until(lambda: exited(old), 15):   # it holds the port until it exits
                raise GuestError("workspace SSH listener did not stop")
        subprocess.run(["/usr/sbin/sshd"], check=True, timeout=30)
    if not wait_until(lambda: listener_alive(), 15):
        raise GuestError("workspace SSH listener did not start again")


def exited(pid):
    """Whether the process is gone, or a zombie (no exe, no sockets)."""
    try:
        os.readlink(PROC / str(pid) / "exe")
    except OSError:
        return True
    return False


def listener_alive():
    try:
        return str((PROC / str(listener_pid()) / "exe").resolve()) == "/usr/sbin/sshd"
    except (OSError, ValueError, subprocess.SubprocessError, GuestError):
        return False


def identity(workspace, ssh_port):
    if os.geteuid() != 0 or not re.fullmatch(r"[a-f0-9]{32}", workspace):
        raise GuestError("fresh workspace SSH identity requires an owned logical identity")
    os.umask(0o077)
    public = SSH / "ssh_host_ed25519_key.pub"
    with open(IDENTITY_LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read_json(IDENTITY)
        if previous.get("workspace_id") == workspace and previous.get("phase") == "ready":
            recorded = previous.get("ed25519_public")
            if public.read_text().split()[:2] != recorded:
                restore_host_keys(recorded)
            elif not (KEPT_KEYS / "ssh_host_ed25519_key.pub").exists():
                keep_host_keys()   # a workspace from before the copy was kept
            key_permissions(SSH)
            if not serves(recorded):   # stopped, or still the cold boot's key after a failed restore
                restart_listener()
                if not wait_until(lambda: serves(recorded), 15):
                    raise GuestError("workspace SSH listener does not serve its identity")
            return
        sessions = subprocess.run(["ss", "-Htn", "state", "established", f"( sport = :{ssh_port} )"],
                                  capture_output=True, text=True, check=True, timeout=10)
        if sessions.stdout.strip():
            raise GuestError("fresh workspace has an unexpected SSH session")
        pid = listener_pid()
        if str((PROC / str(pid) / "exe").resolve()) == "/usr/sbin/sshd (deleted)":
            # sshd started before its binary was replaced (an upgrade, a disk
            # restored under a running VM): no session yet, so start it again
            # from the binary there now.
            restart_listener()
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
        if not wait_until(lambda: serves(expected), 15):
            raise GuestError("workspace SSH listener did not adopt its fresh identity")
        keep_host_keys()
        write_json(IDENTITY, {"workspace_id": workspace, "phase": "ready", "ed25519_public": expected})


def serves(expected):
    """Whether sshd on this machine answers with the ed25519 key expected."""
    scanned = subprocess.run(["ssh-keyscan", "-T", "1", "-t", "ed25519", "127.0.0.1"],
                             capture_output=True, text=True, timeout=3)
    return any(line.split()[1:3] == expected for line in scanned.stdout.splitlines() if not line.startswith("#"))


def keep_host_keys():
    """A copy of the workspace's host keys only root reads, taken once sshd serves them."""
    KEPT_KEYS.mkdir(mode=0o700, exist_ok=True)
    KEPT_KEYS.chmod(0o700)
    for kind in HOST_KEYS:
        for name in (f"ssh_host_{kind}_key", f"ssh_host_{kind}_key.pub"):
            shutil.copy2(SSH / name, KEPT_KEYS / f".{name}.new")
            os.replace(KEPT_KEYS / f".{name}.new", KEPT_KEYS / name)
    key_permissions(KEPT_KEYS)


def restore_host_keys(recorded):
    """A machine that booted with new host keys (a runtime whose resume is a
    cold boot) gets the workspace's own back, so its identity and everyone's
    known_hosts stay valid. Only keys matching the recorded identity are put
    back; anything else is still refused."""
    kept = KEPT_KEYS / "ssh_host_ed25519_key.pub"
    if not recorded or not kept.exists() or kept.read_text().split()[:2] != recorded:
        raise GuestError("workspace SSH identity changed after initialization")
    for kind in HOST_KEYS:
        for name in (f"ssh_host_{kind}_key", f"ssh_host_{kind}_key.pub"):
            shutil.copy2(KEPT_KEYS / name, SSH / f".{name}.sbx")
            os.replace(SSH / f".{name}.sbx", SSH / name)
    key_permissions(SSH)
    restart_listener()


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
# Its certificates are one store (RENEWED), which sbx writes after each
# runtime write, on resume and before they run out, and the forwarder itself
# when it pulls new ones; each new connection takes the one there. With less than
# RENEW_BEFORE left it asks sbx-control for new ones in the background, one
# request at a time, backing off after a failure; a connection waits for that
# only when its certificate has run out. Open connections keep the
# certificate they began with. One thread pumps each connection, never
# shutting the TLS side down half way: when either side ends, what is
# pending goes out and both close. It reads the config again every few
# seconds (endpoints come and go) and exits once the config has been gone
# for a while; a port it can't listen on is in FORWARD_STATUS, which sbx
# reads.

FORWARD_PID = "forwarder.json"          # in RUN: its pid and start time
FORWARD_STATUS = "forwarder-status.json"  # in RUN: {"unbound": {name: port}}
FORWARD_POLL = 2.0
FORWARD_GONE = 15.0                     # seconds without a config before it exits
RENEWED = "endpoint-certs"              # in RUN: BUNDLE files and BUNDLE_INDEX
BUNDLE = "{}.bundle.pem"                # an endpoint's key and certificate in one file, replaced whole
BUNDLE_INDEX = "bundles.json"           # {name: not_after}
RENEW_BEFORE = 3600                     # seconds left on a certificate that start a renewal
RENEW_RETRY = 60                        # seconds before a failed renewal is tried again, doubling
RENEW_RETRY_MAX = 900
RENEW_WAIT = 20                         # how long a connection with a run-out certificate waits for one
BIND_LOG_SECONDS = 300                  # a port that can't be listened on is said again this often
PUMP_CHUNK = 65536
PUMP_BUFFER = 1 << 20                   # bytes held for a side that doesn't take them yet


def _write_private(path, text):
    """A file only its owner reads, replaced whole (a reader sees the old or the new)."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _certificate(endpoint, renewed):
    """(the endpoint's bundle, its not_after); not_after 0 when there is none yet."""
    not_after = read_json(Path(renewed) / BUNDLE_INDEX).get(endpoint["name"])
    bundle = Path(renewed) / BUNDLE.format(endpoint["name"])
    return (str(bundle), float(not_after)) if not_after and bundle.exists() else (str(bundle), 0.0)


def renew(config, folder, renewed):
    """New certificates for every endpoint the sandbox may use, from
    sbx-control (its callback, with the sandbox's credential), into renewed:
    each key and certificate in one file, replaced whole. Dropped when sbx
    pushed others meanwhile (from a CA made anew, which the answer may predate)."""
    import ssl
    import urllib.error
    import urllib.request
    renewed = Path(renewed)
    before = read_json(renewed / BUNDLE_INDEX)
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
    times = dict(read_json(renewed / BUNDLE_INDEX))
    if times != before:
        return times
    renewed.mkdir(mode=0o700, parents=True, exist_ok=True)
    for item in found:
        _write_private(renewed / BUNDLE.format(item["name"]), item["key"] + item["cert"])
        times[item["name"]] = item["not_after"]
    write_json(renewed / BUNDLE_INDEX, times)
    return times


class Renewer:
    """One renewal at a time, in the background; after a failure, none
    before the backoff passed."""

    def __init__(self, config_path, renewed):
        import threading
        self.config_path, self.renewed = Path(config_path), Path(renewed)
        self.lock, self.done = threading.Lock(), threading.Event()
        self.done.set()
        self.next_try, self.backoff = 0.0, RENEW_RETRY

    def start(self) -> bool:
        import threading
        with self.lock:
            if not self.done.is_set() or time.monotonic() < self.next_try:
                return False
            self.done.clear()
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        try:
            config = json.loads(self.config_path.read_text())
            if not config.get("renew"):
                return
            renew(config, self.config_path.parent, self.renewed)
            self.next_try, self.backoff = 0.0, RENEW_RETRY
        except (OSError, ValueError, KeyError) as error:
            say(f"forward: renewing certificates: {type(error).__name__}: {error}; again in {self.backoff} s")
            self.next_try = time.monotonic() + self.backoff
            self.backoff = min(self.backoff * 2, RENEW_RETRY_MAX)
        finally:
            self.done.set()

    def wait(self, timeout):
        return self.done.wait(timeout)


def forward_context(config_path, endpoint, renewed, renewer=None, now=None):
    """The TLS client context for a new connection to the endpoint, with its
    newest certificate. With less than RENEW_BEFORE left a renewal starts in
    the background; only a certificate that has run out waits for it."""
    import ssl
    config = json.loads(Path(config_path).read_text())
    folder = Path(config_path).parent
    renewer = renewer or Renewer(config_path, renewed)
    bundle, not_after = _certificate(endpoint, renewed)
    if not_after - (time.time() if now is None else now) < RENEW_BEFORE and config.get("renew"):
        renewer.start()
        if not_after <= (time.time() if now is None else now):
            renewer.wait(RENEW_WAIT)
            bundle, not_after = _certificate(endpoint, renewed)
    trust = config.get("trust")
    context = ssl.create_default_context(cafile=str(folder / trust) if trust else None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(bundle)
    return context


def forward_dial(address, port, timeout=10):
    import socket
    return socket.create_connection((address, port), timeout=timeout)


def pump(client, server):
    """Both directions of one connection in this thread, non-blocking: the
    app's plain socket and the TLS socket to the endpoint. When either side
    ends or fails, what is pending for the other still goes out, then both
    close. The TLS socket is never shut down half way (that would leave it
    a plain socket); it is only closed."""
    import selectors
    import ssl
    client.setblocking(False)
    server.setblocking(False)
    up, down = bytearray(), bytearray()      # app -> endpoint, endpoint -> app
    ended = False
    selector = selectors.DefaultSelector()
    try:
        while True:
            if ended and not up and not down:
                return
            # TLS may hold decrypted bytes the selector can't see.
            server_ready = not ended and len(down) < PUMP_BUFFER and server.pending() > 0
            events = {client: (selectors.EVENT_READ if not ended and len(up) < PUMP_BUFFER else 0)
                      | (selectors.EVENT_WRITE if down else 0),
                      server: (selectors.EVENT_READ if not ended and len(down) < PUMP_BUFFER else 0)
                      | (selectors.EVENT_WRITE if up else 0)}
            for sock, mask in events.items():
                if mask:
                    try:
                        selector.modify(sock, mask)
                    except KeyError:
                        selector.register(sock, mask)
                else:
                    try:
                        selector.unregister(sock)
                    except KeyError:
                        pass
            if not selector.get_map() and not server_ready:
                return
            ready = {key.fileobj: mask for key, mask in selector.select(timeout=0 if server_ready else None)}
            if server_ready:
                ready[server] = ready.get(server, 0) | selectors.EVENT_READ
            if ready.get(client, 0) & selectors.EVENT_READ:
                try:
                    data = client.recv(PUMP_CHUNK)
                    if data:
                        up += data
                    else:
                        ended = True
                except BlockingIOError:
                    pass
            if ready.get(server, 0) & selectors.EVENT_READ:
                try:
                    data = server.recv(PUMP_CHUNK)
                    if data:
                        down += data
                    else:
                        ended = True
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                    pass
                except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
                    ended = True
            if up and ready.get(server, 0) & selectors.EVENT_WRITE:
                try:
                    del up[:server.send(up)]
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                    pass
            if down and ready.get(client, 0) & selectors.EVENT_WRITE:
                try:
                    del down[:client.send(down)]
                except BlockingIOError:
                    pass
    except (OSError, ValueError):
        return   # a side failed: nothing more can go through
    finally:
        selector.close()
        for sock in (client, server):
            try:
                sock.close()
            except OSError:
                pass


def forward_one(client, endpoint, config_path, renewed, renewer=None):
    """One app connection, through TLS to the endpoint, until either side ends."""
    try:
        context = forward_context(config_path, endpoint, renewed, renewer)
        server = context.wrap_socket(forward_dial(endpoint["address"], endpoint["port"]),
                                     server_hostname=endpoint["address"])
    except (OSError, ValueError) as error:
        say(f"forward {endpoint.get('name')}: {type(error).__name__}: {error}")
        client.close()
        return
    pump(client, server)


class Forwarder:
    """The listeners, kept to what the config says (reconcile)."""

    def __init__(self, config_path, host="127.0.0.1", renewed=None, status=None):
        self.config_path, self.host = str(config_path), host
        self.renewed = Path(renewed) if renewed else RUN / RENEWED
        self.status = Path(status) if status else RUN / FORWARD_STATUS
        self.renewer = Renewer(self.config_path, self.renewed)
        self.listeners = {}   # local_port -> (socket, endpoint)
        self.unbound = {}     # endpoint name -> port it can't listen on
        self.said = {}        # (name, port) -> when that was last said

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
        unbound = {}
        for port, endpoint in wanted.items():
            if port in self.listeners:
                self.listeners[port] = (self.listeners[port][0], endpoint)
                continue
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((self.host, port))
            except OSError as error:
                listener.close()
                unbound[endpoint.get("name", str(port))] = port
                key = (endpoint.get("name"), port)
                if time.monotonic() - self.said.get(key, -BIND_LOG_SECONDS) >= BIND_LOG_SECONDS:
                    say(f"forward {endpoint.get('name')}: can't listen on {self.host}:{port}: {error}")
                    self.said[key] = time.monotonic()
                continue
            listener.listen(64)
            self.listeners[port] = (listener, endpoint)
            threading.Thread(target=self._accept, args=(port, listener), daemon=True).start()
        if unbound != self.unbound:
            self.unbound = unbound
            try:
                write_json(self.status, {"unbound": unbound})
            except OSError:
                pass
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
            threading.Thread(target=forward_one, args=(client, found[1], self.config_path, self.renewed, self.renewer),
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


def forward(action, config_path, state_file=None):
    """ensure: start the forwarder (detached) unless one runs. run: be it."""
    state_file = Path(state_file) if state_file else RUN / FORWARD_PID
    if action == "run":
        write_json(state_file, record(os.getpid()))
        Forwarder(config_path).run()
        return
    with open(f"{state_file}.lock", "a") as lock:   # two ensures at once start one
        fcntl.flock(lock, fcntl.LOCK_EX)
        if alive(read_json(state_file)):
            return
        with open("/tmp/sbx-endpoints.log", "a") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "forward", "run",
                                        str(config_path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=log, start_new_session=True)
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
