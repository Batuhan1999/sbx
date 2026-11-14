import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from support import Installation


def certificate(address="203.0.113.10", expired=False):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address)])
    return (x509.CertificateBuilder().subject_name(name)
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test certificate issuer")]))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now - timedelta(hours=1) if expired else now + timedelta(hours=160))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), critical=False)
            .sign(key, hashes.SHA256()))


class CertificateActivationTests(Installation):
    """The IP address is ready for setup once nginx serves a certificate for it."""
    SETTINGS = {"domain": {"address": "203.0.113.10"}}

    def test_ip_certificate_requires_matching_san_and_activation_fingerprint(self):
        from sbx_control import setup
        with tempfile.TemporaryDirectory(prefix="sbx-ip-cert-") as folder:
            target = Path(folder) / "letsencrypt/live/203.0.113.10"
            target.mkdir(parents=True)
            cert = certificate()
            (target / "fullchain.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            marker = Path(folder) / "ip-certificate.fingerprint"   # what tls.sh writes once nginx loaded it
            with patch("sbx_control.paths.tls", return_value=Path(folder)):
                self.assertFalse(setup.address_ready())
                marker.write_text("sha256 Fingerprint=wrong")
                self.assertFalse(setup.address_ready())
                marker.write_text("sha256 Fingerprint=" + cert.fingerprint(hashes.SHA256()).hex())
                self.assertTrue(setup.address_ready())
                wrong = certificate("203.0.113.11")
                (target / "fullchain.pem").write_bytes(wrong.public_bytes(serialization.Encoding.PEM))
                marker.write_text(wrong.fingerprint(hashes.SHA256()).hex())
                self.assertFalse(setup.address_ready())


class TLSServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-tls-service-"))
        self.root = Path(self.folder)
        self.bin = self.root / "bin"
        self.control = self.root / "control"
        for folder in (self.bin, self.control / "venv/bin", self.root / "systemd"):
            folder.mkdir(parents=True)
        self.fixture = self.root / "fixture.pem"
        self.fixture.write_bytes(certificate().public_bytes(serialization.Encoding.PEM))
        self.log = self.root / "commands.jsonl"
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                    "SBX_TEST_ROOT": str(self.root), "SBX_TEST_DOMAIN": "", "SBX_TEST_FAIL": ""}
        self.script = self.root / "tls.sh"
        source = (Path(__file__).parents[1] / "tls.sh").read_text()
        for original, replacement in (("/opt/sbx-control", str(self.control)), ("/opt/sbx-tls", str(self.root / "tls")),
                                      ("/usr/local/sbin", str(self.bin)), ("/etc/systemd/system", str(self.root / "systemd")),
                                      ("/var/cache/sbx-gate", str(self.root / "cache"))):
            source = source.replace(original, replacement)
        self.script.write_text(source)
        self.write_command(self.control / "venv/bin/python", '''
import shlex
if sys.argv[1:2] == ['-c'] and 'host.CURRENT' in sys.argv[2]:
    print(os.getcwd())
    raise SystemExit(0)
if sys.argv[1:4] == ['-m', 'sbx_control.ip_previews', 'nginx']:
    from sbx_control.ip_previews import nginx
    print(nginx(sys.argv[4], sys.argv[5]), end='')
    raise SystemExit(0)
domain = os.environ["SBX_TEST_DOMAIN"]
values = {"domain": domain, "login": "login." + domain, "bootstrap_address": "203.0.113.10",
          "email": "operator@example.com", "dns": "delegated", "services": ""}
for name, value in values.items():
    print(name + "=" + shlex.quote(value))
''')
        self.write_command(self.bin / "docker", '''
with (root / "commands.jsonl").open("a") as log:
    log.write(json.dumps(["docker", *sys.argv[1:]]) + "\\n")
if "certonly" in sys.argv:
    if os.environ.get("SBX_TEST_FAIL"):
        print("HTTP validation timed out on port 80", file=sys.stderr)
        sys.exit(1)
    name = sys.argv[sys.argv.index("--cert-name") + 1]
    target = root / "tls/letsencrypt/live" / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "fullchain.pem").write_bytes((root / "fixture.pem").read_bytes())
    (target / "privkey.pem").write_text("test fixture key")
''')
        self.write_command(self.control / "venv/bin/certbot", '''
name = sys.argv[sys.argv.index("--cert-name") + 1]
target = root / "tls/letsencrypt/live" / name
target.mkdir(parents=True, exist_ok=True)
(target / "fullchain.pem").write_bytes((root / "fixture.pem").read_bytes())
(target / "privkey.pem").write_text("test fixture key")
''')
        self.write_command(self.bin / "systemctl", '''
with (root / "commands.jsonl").open("a") as log:
    log.write(json.dumps(["systemctl", *sys.argv[1:]]) + "\\n")
if sys.argv[1:] == ["start", "sbx-tls-renew.service"]:
    result = subprocess.run([str(root / "bin/sbx-tls-renew")])
    sys.exit(result.returncode)
''')
        # The host script targets Linux sed; emulate that invocation on macOS too.
        self.write_command(self.bin / "sed", '''
expression, filename = sys.argv[2:]
_, before, after, _ = expression.split("@")
path = Path(filename)
path.write_text(path.read_text().replace(before, after))
''')

    def write_command(self, path, body):
        path.write_text(f"#!{sys.executable}\nimport os, sys, json, subprocess\nfrom pathlib import Path\n"
                        "root = Path(os.environ['SBX_TEST_ROOT'])\n" + body)
        path.chmod(0o700)

    def run_service(self):
        return subprocess.run(["sh", str(self.script)], cwd=self.control, env=self.env,
                              capture_output=True, text=True, timeout=30)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_ip_issuance_reloads_nginx_and_installs_renewal_timer(self):
        result = self.run_service()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_service().returncode, 0)   # and again, as the renewal timer does
        requests = [c for c in self.commands() if "certonly" in c]
        self.assertEqual(len(requests), 2)
        for request in requests:
            # Let's Encrypt issues IP certificates only short-lived; a rerun must not spend its rate limit.
            self.assertEqual((request[request.index("--ip-address") + 1],
                              request[request.index("--preferred-profile") + 1]), ("203.0.113.10", "shortlived"))
            self.assertNotIn("--force-renewal", request)
        self.assertIn("/live/203.0.113.10/fullchain.pem", (self.root / "tls/nginx/default.conf").read_text())
        self.assertIn("/live/203.0.113.10/fullchain.pem", (self.root / "tls/nginx/ip-previews.conf").read_text())
        self.assertTrue((self.root / "tls/ip-certificate.fingerprint").exists())
        self.assertIn(["systemctl", "enable", "-q", "--now", "sbx-tls-renew.timer"], self.commands())
        self.assertIn(["systemctl", "restart", "sbx-host-guard"], self.commands())   # port 80 opens for validation

    def test_failed_ip_validation_keeps_bootstrap_and_exposes_retry_detail(self):
        self.env["SBX_TEST_FAIL"] = "1"
        result = self.run_service()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("port 80", (self.root / "tls/ip-certificate-error").read_text())
        self.assertIn("/live/bootstrap/fullchain.pem", (self.root / "tls/nginx/default.conf").read_text())
        self.assertFalse((self.root / "tls/ip-certificate.fingerprint").exists())
        self.env["SBX_TEST_FAIL"] = ""
        self.assertEqual(self.run_service().returncode, 0)
        self.assertFalse((self.root / "tls/ip-certificate-error").exists())

    def test_adding_domain_keeps_ip_renewal_and_ip_certificate_as_tls_default(self):
        self.assertEqual(self.run_service().returncode, 0)
        ip_requests = sum("certonly" in c for c in self.commands())
        self.env["SBX_TEST_DOMAIN"] = "sbx.example.com"
        result = self.run_service()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.root / "tls/nginx/default.conf").read_text()
        bootstrap = (self.root / "tls/nginx/bootstrap.conf").read_text()
        self.assertIn("/live/sbx.example.com/fullchain.pem", config)
        self.assertNotIn("listen 443 ssl default_server", config)
        self.assertIn("listen 443 ssl default_server", bootstrap)
        self.assertIn("/live/203.0.113.10/fullchain.pem", bootstrap)
        self.assertGreater(sum("certonly" in c for c in self.commands()), ip_requests)   # the domain's renewal renews the IP's too
        self.assertFalse((self.root / 'tls/nginx/ip-previews.conf').exists())
        # A deleted sandbox's preview is a 404 within a second, not a 502 for as long as an allow is cached.
        self.assertIn("proxy_cache_valid 200 1s;", (self.root / "tls/nginx/gate.inc").read_text())


if __name__ == "__main__":
    unittest.main()
