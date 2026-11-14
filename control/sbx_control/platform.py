"""The platform context: the sandbox runtime's credentials, the session key,
the ports, paths and host names the platform itself uses, the start contract
every template runs.

Nothing here runs at import. Each entry point (the server's lifespan, a host
CLI) calls connect() once; code that needs the credentials later asks
credentials()."""
import os
from dataclasses import dataclass

from . import state, vault

# In the sandbox:
SSH_PORT = 22
VNC_PORTS = (5900, 5901)    # VNC's default display port, and the desktop's x11vnc (START)
VNC_PORT = VNC_PORTS[1]
NOVNC_PORT = 6080           # websockify in front of VNC, the desktop preview
SSH_BRIDGE_PORT = 8081      # the CLI's SSH-over-WebSocket bridge (websocat in START)
ENVD_PORT = 49983           # the sandbox's control daemon; it checks its own access token
# Never a detected preview, even where the operator removed the preview that declares it.
# Extensions add their own (extensions.Connector.ports).
PLATFORM_PORTS = frozenset({SSH_PORT, *VNC_PORTS, NOVNC_PORT, SSH_BRIDGE_PORT, ENVD_PORT})
# On the host:
CONTROL_PORT = 8080         # sbx-control, on loopback behind nginx
CLIENT_PROXY_PORT = 3002    # Embed's client-proxy, which reaches <port>-<sandbox id> hosts
# The <port>-<id> host suffix client-proxy routes when there is no domain.
INTERNAL_HOST_SUFFIX = "e2b.sandboxes.internal"
SESSION_COOKIE = "sbx_session"
# The platform's tmpfs in the sandbox: env, keys, the shipped recipe, the
# guest helper (core.ship_guest), what extensions write.
RUN = "/run/sbx"
APP_SCRIPTS = f"{RUN}/app"  # the recipe's .sbx/ as shipped (recipes.ship)
# The sandbox user's home (E2B's user, "user"): checkouts, SSH keys, agents' state.
HOME = "/home/user"

# Every template's start command, also run once at build before the snapshot.
# The client's SSH ProxyCommand dials the websocat bridge through client-proxy.
START = rf"""bash -c '
if ! ssh-keyscan -T 1 -t ed25519 127.0.0.1 2>/dev/null | grep -q " ssh-ed25519 "; then
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
nohup Xvfb :0 -screen 0 1600x1000x24 >/tmp/xvfb.log 2>&1 &
sleep 1
DISPLAY=:0 nohup openbox >/tmp/openbox.log 2>&1 &
DISPLAY=:0 xsetroot -solid "#2e3440"
nohup x11vnc -display :0 -forever -shared -nopw -localhost -rfbport {VNC_PORT} -quiet >/tmp/x11vnc.log 2>&1 &
nohup websockify --web /opt/novnc {NOVNC_PORT} localhost:{VNC_PORT} >/tmp/novnc.log 2>&1 &
sleep infinity'"""
# Whether START's transport is up: what a cold-booted VM (booted from its disk) lacks.
STARTED = ("ssh-keyscan -T 1 -t ed25519 127.0.0.1 2>/dev/null | grep -q ' ssh-ed25519 ' "
           f"&& ss -Hltn 'sport = :{NOVNC_PORT}' | grep -q . && echo ready || true")


@dataclass(frozen=True)
class Credentials:
    """The optional GitHub deploy key, and every platform secret by its env
    name (vault.platform_secrets)."""
    github_key: str
    all: dict


_connected: Credentials | None = None


def connect() -> Credentials:
    """Read the platform secrets and point the E2B SDK at Embed. install.sh
    copies the key and URLs from Embed's sdk.env. Also called again when the
    secrets store reports a change."""
    global _connected
    found = vault.platform_secrets()
    if "E2B_API_KEY" not in found or not ({"E2B_API_URL", "E2B_HOST"} & found.keys()):
        raise SystemExit("missing platform secrets: E2B_API_KEY and E2B_API_URL (/etc/sbx/runtime.env, written by install.sh)")
    host = found.get("E2B_HOST", "")
    # The SDK reads these on every call; this is the one place that sets them.
    os.environ["E2B_API_KEY"] = found["E2B_API_KEY"]
    os.environ["E2B_API_URL"] = found.get("E2B_API_URL") or f"http://{host}:3000"
    # A sandbox. hostname makes client-proxy route by header (proxy/host.go).
    os.environ["E2B_SANDBOX_URL"] = found.get("E2B_SANDBOX_URL") or f"http://sandbox.{host}:{CLIENT_PROXY_PORT}"
    key = found.get("GITHUB_DEPLOY_KEY", "").strip()
    _connected = Credentials(key + "\n" if key else "", found)
    return _connected


def credentials() -> Credentials:
    return _connected or connect()


def session_key() -> str:
    return state.session_key()
