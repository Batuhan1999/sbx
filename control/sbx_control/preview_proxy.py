"""Loopback-only app access for an operator connected to a domainless host."""
import json
import ipaddress
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit

from . import addressing, core, platform


def fixed_routing_host(value):
    """The runtime honors routing headers for IP/localhost, not arbitrary names."""
    try:
        host = value.decode("ascii")
        parsed = urlsplit("http://" + host)
        if parsed.netloc != host or parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        if parsed.hostname == "localhost":
            return host == "localhost" or host.startswith("localhost:")
        if not parsed.hostname or "%" in parsed.hostname:
            return False
        ipaddress.ip_address(parsed.hostname)
        return True
    except (UnicodeError, ValueError):
        return False


class PreviewServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, listen, sandbox_id, port, upstream=("127.0.0.1", platform.CLIENT_PROXY_PORT), ensure_running=lambda: None,
                 *, preserve_host=False):
        self.sandbox_id, self.port, self.upstream = sandbox_id, port, upstream
        self.ensure_running = ensure_running
        self.preserve_host = preserve_host
        super().__init__(("127.0.0.1", listen), PreviewConnection)


class PreviewConnection(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(15)
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                part = client.recv(8192)
                if not part:
                    return
                data += part
                if len(data) > 65536:
                    client.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                    return
            headers, body = data.split(b"\r\n\r\n", 1)
            lines = headers.split(b"\r\n")
            # One backend connection per HTTP request, or one upgraded WebSocket.
            # The fixed routing headers never come from the browser.
            upgrade = any(line.lower().startswith(b"upgrade:") for line in lines[1:])
            excluded = {b"host", b"e2b-sandbox-id", b"e2b-sandbox-port", b"x-forwarded-host",
                        b"x-forwarded-proto", b"connection", b"proxy-connection"}
            original_host = next((line.partition(b":")[2].strip() for line in lines[1:]
                                  if line.partition(b":")[0].lower() == b"host"), b"localhost")
            if self.server.preserve_host and not fixed_routing_host(original_host):
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                return
            lines = [lines[0], *(line for line in lines[1:] if line.partition(b":")[0].lower() not in excluded)]
            lines.extend([
                b"Host: " + (original_host if self.server.preserve_host else
                             f"{self.server.port}-{self.server.sandbox_id}.{platform.INTERNAL_HOST_SUFFIX}".encode()),
                f"E2b-Sandbox-Id: {self.server.sandbox_id}".encode(),
                f"E2b-Sandbox-Port: {self.server.port}".encode(),
                b"X-Forwarded-Host: " + original_host, b"X-Forwarded-Proto: http",
                b"Connection: upgrade" if upgrade else b"Connection: close",
            ])
            self.server.ensure_running()
            with socket.create_connection(self.server.upstream, timeout=15) as backend:
                backend.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + body)
                client.settimeout(60)
                backend.settimeout(60)
                last_data = time.monotonic()
                while time.monotonic() - last_data < 3600:
                    ready, _, _ = select.select([client, backend], [], [], 30)
                    for source in ready:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        (backend if source is client else client).sendall(chunk)
                        last_data = time.monotonic()
        except (OSError, ValueError):
            return
        except Exception:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")


def serve(name, port, listen, *, preserve_host=False):
    platform.connect()
    info = core.find(name)
    if not info:
        raise ValueError(f"no sandbox {name}")
    app = core.app_of(info)
    port = port or addressing.ports(app).get("")
    if port is None or port in platform.PLATFORM_PORTS or addressing.preview_on(app, port) is None:
        raise ValueError("choose a declared app preview port with --port")
    lock = threading.Lock()
    last_connected = 0

    def ensure_running():
        nonlocal last_connected
        with lock:
            if time.monotonic() - last_connected > 60:
                core.attach(name, "for a loopback preview")
                last_connected = time.monotonic()

    ensure_running()
    with PreviewServer(listen, info.sandbox_id, port, ensure_running=ensure_running,
                       preserve_host=preserve_host) as server:
        print(json.dumps({"name": name, "sandbox_id": info.sandbox_id, "app_port": port,
                          "url": f"http://127.0.0.1:{server.server_address[1]}/",
                          "preserve_host": preserve_host,
                          "access": "host loopback only; forward this port over SSH"}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
