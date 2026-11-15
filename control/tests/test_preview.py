import http.client
import http.server
import socket
import threading
import unittest

from sbx_control.runtime.embed.proxy import PreviewServer, fixed_routing_host


class Backend(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.server.seen = dict(self.headers)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.seen = dict(self.headers)
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.end_headers()
        self.connection.sendall(self.connection.recv(1024))
        self.close_connection = True

    def log_message(self, *_):
        pass


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        self.preview = PreviewServer(0, "test-sandbox", 4319, upstream=self.backend.server_address)
        for server in (self.backend, self.preview):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def test_body_and_routing_cannot_be_redirected_by_browser_headers(self):
        self.assertEqual(self.preview.server_address[0], "127.0.0.1")
        connection = http.client.HTTPConnection(*self.preview.server_address, timeout=5)
        self.addCleanup(connection.close)
        connection.request("POST", "/v1/traces", body=b"synthetic trace", headers={
            "Host": "localhost:8096", "E2b-Sandbox-Id": "other-sandbox", "E2b-Sandbox-Port": "49983",
            "Authorization": "Bearer app-token", "X-Forwarded-Host": "injected.example",
        })
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"synthetic trace")
        self.assertEqual(self.backend.seen["Host"], "4319-test-sandbox.e2b.sandboxes.internal")
        self.assertEqual(self.backend.seen["E2b-Sandbox-Id"], "test-sandbox")
        self.assertEqual(self.backend.seen["E2b-Sandbox-Port"], "4319")
        self.assertEqual(self.backend.seen["X-Forwarded-Host"], "localhost:8096")
        self.assertEqual(self.backend.seen["Authorization"], "Bearer app-token")

    def test_upgrade_remains_bidirectional(self):
        with socket.create_connection(self.preview.server_address, timeout=5) as client:
            client.sendall(b"GET /socket HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: upgrade\r\n\r\n")
            headers = b""
            while not headers.endswith(b"\r\n\r\n"):
                headers += client.recv(1)
            self.assertIn(b"101", headers)
            client.sendall(b"websocket frame")
            self.assertEqual(client.recv(1024), b"websocket frame")

    def preserving_preview(self):
        preview = PreviewServer(0, "test-sandbox", 4319, upstream=self.backend.server_address,
                                preserve_host=True)
        threading.Thread(target=preview.serve_forever, daemon=True).start()
        self.addCleanup(preview.server_close)
        self.addCleanup(preview.shutdown)
        return preview

    def test_origin_bound_auth_preserves_host_and_proof_while_routing_stays_fixed(self):
        preview = self.preserving_preview()
        connection = http.client.HTTPConnection(*preview.server_address, timeout=5)
        self.addCleanup(connection.close)
        connection.request("POST", "/oauth/token", body=b"synthetic native exchange", headers={
            "Host": "127.0.0.1:19296", "DPoP": "synthetic-proof", "Authorization": "DPoP synthetic-session",
            "E2b-Sandbox-Id": "other-sandbox", "E2b-Sandbox-Port": "49983",
            "X-Forwarded-Host": "injected.example", "X-Forwarded-Proto": "https",
        })
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"synthetic native exchange")
        self.assertEqual(self.backend.seen["Host"], "127.0.0.1:19296")
        self.assertEqual(self.backend.seen["DPoP"], "synthetic-proof")
        self.assertEqual(self.backend.seen["Authorization"], "DPoP synthetic-session")
        self.assertEqual(self.backend.seen["E2b-Sandbox-Id"], "test-sandbox")
        self.assertEqual(self.backend.seen["E2b-Sandbox-Port"], "4319")
        self.assertEqual(self.backend.seen["X-Forwarded-Host"], "127.0.0.1:19296")
        self.assertEqual(self.backend.seen["X-Forwarded-Proto"], "http")

    def test_preserve_host_refuses_a_hostname_that_could_override_runtime_header_routing(self):
        preview = self.preserving_preview()
        connection = http.client.HTTPConnection(*preview.server_address, timeout=5)
        self.addCleanup(connection.close)
        connection.request("POST", "/", body=b"body", headers={
            "Host": "5999-other-sandbox.e2b.sandboxes.internal",
            "E2b-Sandbox-Id": "other-sandbox", "E2b-Sandbox-Port": "5999",
        })
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertEqual(response.read(), b"")
        self.assertFalse(hasattr(self.backend, "seen"))

    def test_only_runtime_supported_ip_and_localhost_forms_keep_header_routing(self):
        for host in (b"localhost", b"localhost:19296", b"127.0.0.1:19296", b"[::1]:19296", b"203.0.113.7"):
            self.assertTrue(fixed_routing_host(host), host)
        for host in (b"other.example", b"5999-other-vm.e2b.sandboxes.internal", b"localhost.attacker.example",
                     b"user@localhost", b"localhost:0", b"localhost:65536", b"localhost/path", b"localhost?x=1",
                     b"LOCALHOST:19296", b"[fe80::1%eth0]:19296", b"127.0.0.1:19296,other.example"):
            self.assertFalse(fixed_routing_host(host), host)

    def test_origin_preserving_websocket_keeps_native_proof_and_bidirectional_frames(self):
        preview = self.preserving_preview()
        with socket.create_connection(preview.server_address, timeout=5) as client:
            client.sendall(b"GET /ws?wsTicket=synthetic&dpop=synthetic HTTP/1.1\r\n"
                           b"Host: 127.0.0.1:19296\r\nDPoP: synthetic-proof\r\n"
                           b"Upgrade: websocket\r\nConnection: upgrade\r\n\r\n")
            headers = b""
            while not headers.endswith(b"\r\n\r\n"):
                headers += client.recv(1)
            self.assertIn(b"101", headers)
            self.assertEqual(self.backend.seen["Host"], "127.0.0.1:19296")
            self.assertEqual(self.backend.seen["DPoP"], "synthetic-proof")
            client.sendall(b"opaque native frame")
            self.assertEqual(client.recv(1024), b"opaque native frame")

    def test_oversized_headers_are_rejected(self):
        with socket.create_connection(self.preview.server_address, timeout=5) as client:
            client.sendall(b"GET / HTTP/1.1\r\nX-Long: " + b"a" * 66000)
            self.assertIn(b"431", client.recv(1024))


if __name__ == "__main__":
    unittest.main()
