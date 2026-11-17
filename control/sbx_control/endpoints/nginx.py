"""The host's side (root): nginx's stream configuration for the endpoints,
rendered from the records (render, pure) and applied (apply_host): written,
tested and reloaded, the files before put back when nginx refuses them."""
from __future__ import annotations

import datetime
import subprocess
import sys

from .. import host_requests, paths, settings
from ..tls_front import HTTPS_BACKEND, PORT
from .model import REVISION, Endpoint, domain, routable

ENDPOINTS_BACKEND = "127.0.0.1:8441"    # nginx's TLS servers for the endpoints, behind the stream front on PORT
ENDPOINT_IDLE = "1h"                    # an endpoint connection with no traffic this long is closed
STREAM_FILE = "endpoints.stream"        # in tls.sh's nginx folder, included in nginx's stream block
CA_FILES = "endpoint-{}-ca.pem"         # each endpoint's CA certificate, beside it
NGINX = "/usr/local/sbin/sbx-tls-nginx"  # tls.sh's: start nginx once, test and reload its configuration


def stream_config(rows: list[Endpoint], name: str) -> str:
    """The front on PORT, which reads the TLS server name without
    terminating (ssl_preread) and sends an endpoint's <name>.<domain> to
    ENDPOINTS_BACKEND and everything else to the HTTPS server on
    HTTPS_BACKEND, both with the client's address in the PROXY protocol;
    and a server per endpoint (rows: the routed ones) on ENDPOINTS_BACKEND,
    chosen by server_name, verifying clients against that endpoint's own CA."""
    routes = "".join(f"  {r.name}.{name} {ENDPOINTS_BACKEND};\n" for r in rows)
    out = [f"""# sbx's front on 443 (endpoints): the TLS server name picks the backend, nothing is
# decrypted here. Endpoints go to their TLS servers below; everything else to the HTTPS
# server (tls.sh), which takes the client's address from the PROXY protocol.
map $ssl_preread_server_name $sbx_backend {{
  default {HTTPS_BACKEND};
{routes}}}
server {{
  listen {PORT};
  ssl_preread on;
  proxy_pass $sbx_backend;
  proxy_protocol on;
  proxy_connect_timeout 10s;
  proxy_timeout 1d;
}}"""]
    if rows:
        out.append(f"""# Endpoints: TLS with the installation's certificate; a server per endpoint, chosen by
# SNI, verifies the client against that endpoint's own CA. Any other name is rejected.
server {{
  listen {ENDPOINTS_BACKEND} ssl proxy_protocol;
  ssl_reject_handshake on;
}}""")
    for row in rows:
        out.append(f"""server {{
  listen {ENDPOINTS_BACKEND} ssl proxy_protocol;
  server_name {row.name}.{name};
  ssl_certificate /etc/letsencrypt/live/{name}/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/{name}/privkey.pem;
  ssl_protocols TLSv1.2 TLSv1.3;
  ssl_client_certificate /etc/nginx/conf.d/{CA_FILES.format(row.name)};
  ssl_verify_client on;
  ssl_verify_depth 1;
  ssl_handshake_timeout 10s;
  proxy_pass {row.target};
  proxy_connect_timeout 10s;
  proxy_timeout {ENDPOINT_IDLE};
}}""")
    return "\n".join(out) + "\n"


def render(rows: list[Endpoint], name: str) -> dict:
    """file name -> text: the stream configuration and each routed endpoint's CA certificate."""
    return {STREAM_FILE: stream_config(rows, name), **{CA_FILES.format(r.name): r.ca_cert for r in rows}}


def _held(folder) -> dict:
    """The files of render's that are in the folder now."""
    names = [STREAM_FILE, *(p.name for p in folder.glob(CA_FILES.format("*")))]
    return {n: (folder / n).read_text() for n in names if (folder / n).exists()}


def replace(folder, files: dict) -> dict:
    """The folder's render files made these; the ones before."""
    before = _held(folder)
    for name in before.keys() - files.keys():
        (folder / name).unlink()
    for name, text in files.items():
        (folder / name).write_text(text)
    return before


def write_nginx(folder):
    """The configuration from the records now, in nginx's folder (tls.sh)."""
    replace(folder, render(routable(), domain()))


def applied() -> dict:
    """The root side's last apply (its host_requests record): {revision, ok, message, ok_revision, at}."""
    return host_requests.last(settings.HostUnit.ENDPOINTS)


def apply_host() -> int:
    """sbx-apply endpoints, as root: nginx's stream configuration from the
    records now, nginx tested and reloaded (the configuration before kept
    when it fails). Its outcome in its host_requests record, which the API
    reads; nginx's own words go to the journal only."""
    folder = paths.tls() / "nginx"
    revision = int(REVISION.get("0"))
    record = applied()
    try:
        if not (folder.is_dir() and (paths.tls() / "nginx.conf").exists()):
            raise RuntimeError("nginx is not set up on this server yet (connect a domain first)")
        before = _held(folder)
        try:
            replace(folder, render(routable(), domain()))
            result = subprocess.run([NGINX], capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError):   # not written whole, or nginx not run to the end
            replace(folder, before)
            raise
        if result.returncode:
            print(result.stdout + result.stderr, file=sys.stderr, flush=True)
            replace(folder, before)
            subprocess.run([NGINX], capture_output=True, timeout=300)
            raise RuntimeError("nginx did not take the endpoints' configuration; the one before stays "
                               "(journalctl -u sbx-apply-endpoints says why)")
        record = {**record, "revision": revision, "ok": True, "message": "", "ok_revision": revision}
        code = 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        record = {**record, "revision": revision, "ok": False, "message": str(error)}
        print(f"endpoints: {error}", file=sys.stderr, flush=True)
        code = 1
    record["at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    host_requests.record(settings.HostUnit.ENDPOINTS, record)
    return code
