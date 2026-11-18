"""The platform context: the sandbox runtime's credentials, the session key,
the ports, paths and host names the platform itself uses, the start contract
every template runs.

Nothing here runs at import. Each entry point (the server's lifespan, a host
CLI) calls connect() once; code that needs the credentials later asks
credentials()."""
from dataclasses import dataclass

from . import state, vault

# In the sandbox:
SSH_PORT = 22
VNC_PORTS = (5900, 5901)    # VNC's default display port, and the desktop's x11vnc (START)
VNC_PORT = VNC_PORTS[1]
NOVNC_PORT = 6080           # websockify in front of VNC, the desktop preview
SSH_BRIDGE_PORT = 8081      # the CLI's SSH-over-WebSocket bridge (websocat in START)
# Never a detected preview, even where the operator removed the preview that declares it.
# The runtime adds its own (runtime.Runtime.ports), extensions theirs (extensions.Connector.ports).
PLATFORM_PORTS = frozenset({SSH_PORT, *VNC_PORTS, NOVNC_PORT, SSH_BRIDGE_PORT})
# On the host:
CONTROL_PORT = 8080         # sbx-control, on loopback behind nginx
SESSION_COOKIE = "sbx_session"
# The platform's tmpfs in the sandbox: env, keys, the shipped recipe, the
# guest helper (core.ship_guest), what extensions write.
RUN = "/run/sbx"
APP_SCRIPTS = f"{RUN}/app"  # the recipe's .sbx/ as shipped (recipes.ship)
# The sandbox user's home (the runtime's default user, "user"): checkouts, SSH keys, agents' state.
HOME = "/home/user"

# Every template's start command, also run once at build before the snapshot.
# The client's SSH ProxyCommand dials the websocat bridge through client-proxy.
# The transport (sshd, the SSH bridge) runs on every runtime; sbx's desktop
# (Xvfb, x11vnc, noVNC on NOVNC_PORT) only where the runtime has none of its
# own (Runtime.desktop "sbx"): a provider's desktop keeps its ports.
_TRANSPORT = rf"""if ! ssh-keyscan -T 1 -t ed25519 127.0.0.1 2>/dev/null | grep -q " ssh-ed25519 "; then
  if [ "$(cat /proc/1/comm)" = systemd ]; then
    # Stop boot-time retries before creating keys; systemd owns /run/sshd.
    sudo systemctl stop ssh.socket ssh.service || exit 1
    sudo ssh-keygen -A >/dev/null 2>&1 || exit 1
    sudo systemctl reset-failed ssh.socket ssh.service || exit 1
    sudo systemctl start ssh.service || exit 1
  else
    sudo mkdir -p /run/sshd && sudo ssh-keygen -A >/dev/null 2>&1 && sudo /usr/sbin/sshd || exit 1
  fi
fi
nohup websocat --binary --exit-on-eof ws-l:0.0.0.0:{SSH_BRIDGE_PORT} tcp:127.0.0.1:{SSH_PORT} >/tmp/websocat.log 2>&1 &
"""
_DESKTOP = rf"""nohup Xvfb :0 -screen 0 1600x1000x24 >/tmp/xvfb.log 2>&1 &
sleep 1
DISPLAY=:0 nohup openbox >/tmp/openbox.log 2>&1 &
DISPLAY=:0 xsetroot -solid "#2e3440"
nohup x11vnc -display :0 -forever -shared -nopw -localhost -rfbport {VNC_PORT} -quiet >/tmp/x11vnc.log 2>&1 &
nohup websockify --web /opt/novnc {NOVNC_PORT} localhost:{VNC_PORT} >/tmp/novnc.log 2>&1 &
"""
_SSH_READY = "ssh-keyscan -T 1 -t ed25519 127.0.0.1 2>/dev/null | grep -q ' ssh-ed25519 '"


def start(own_desktop: bool = True) -> str:
    """The start command: the transport, and sbx's desktop unless the runtime has its own."""
    return "bash -c '\n" + _TRANSPORT + (_DESKTOP if own_desktop else "") + "sleep infinity'"


def started(own_desktop: bool = True) -> str:
    """Whether start's transport (and sbx's desktop) is up: what a cold-booted VM lacks."""
    port = NOVNC_PORT if own_desktop else SSH_BRIDGE_PORT
    return f"{_SSH_READY} && ss -Hltn 'sport = :{port}' | grep -q . && echo ready || true"


def ready_port(own_desktop: bool = True) -> int:
    """The port whose listening says start is up (a template's start command's ready check)."""
    return NOVNC_PORT if own_desktop else SSH_BRIDGE_PORT


START = start()
STARTED = started()


@dataclass(frozen=True)
class Credentials:
    """The optional GitHub deploy key, and every platform secret by its env
    name (vault.platform_secrets)."""
    github_key: str
    all: dict


_connected: Credentials | None = None


def connect() -> Credentials:
    """Read the platform secrets and hand the runtime its own (runtime.env,
    which install.sh writes). Also called again when the secrets store
    reports a change."""
    global _connected
    from . import runtime
    found = vault.platform_secrets()
    runtime.configure(found)
    key = found.get("GITHUB_DEPLOY_KEY", "").strip()
    _connected = Credentials(key + "\n" if key else "", found)
    return _connected


def credentials() -> Credentials:
    return _connected or connect()


def session_key() -> str:
    return state.session_key()
