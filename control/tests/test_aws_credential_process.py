"""Exercise the generated AWS profile with the real SDK over a local TLS endpoint."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[2] / 'catalog/aws-profile/credential-process.py'


class CredentialProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        cls.cert, key = root / 'certificate.pem', root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(key), '-out', str(cls.cert), '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=DNS:localhost'], check=True, capture_output=True)
        cls.received, cls.reply, cls.status = [], {}, 200

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                cls.received.append((self.path, self.headers.get('Authorization')))
                self.send_response(cls.status)
                if cls.status == 302:
                    self.send_header('Location', '/redirect-must-not-receive-credentials')
                self.end_headers()
                self.wfile.write(json.dumps(cls.reply).encode())

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cls.cert), str(key))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        type(self).received.clear()
        type(self).status = 200
        type(self).reply = {'Version': 1, 'AccessKeyId': 'fixture-access-key',
            'SecretAccessKey': 'fixture-private-secret', 'SessionToken': 'fixture-session-token',
            'Expiration': (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()}
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.connection = root / 'connection.json'
        self.url = f'https://localhost:{self.server.server_address[1]}/api/sandbox-credentials/aws/test/aws'
        self.connection.write_text(json.dumps({'url': self.url, 'token': 'fixture-capability'}))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('AWS_')}
        self.env.update(SSL_CERT_FILE=str(self.cert), AWS_EC2_METADATA_DISABLED='true')

    def call_helper(self):
        return subprocess.run([sys.executable, str(HELPER), str(self.connection)],
                              env=self.env, text=True, capture_output=True, timeout=10)

    def test_real_aws_sdk_uses_and_refreshes_the_named_profile(self):
        import boto3
        config = self.connection.parent / 'config'
        config.write_text('[profile dev]\nregion = eu-central-1\ncredential_process = '
            f'"{sys.executable}" "{HELPER}" "{self.connection}"\n')
        self.env.update(AWS_CONFIG_FILE=str(config), AWS_SHARED_CREDENTIALS_FILE=str(config) + '.empty')
        # A session that expires within the SDK's refresh window is renewed through the helper on next use.
        type(self).reply['Expiration'] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with patch.dict(os.environ, self.env, clear=True):
            credentials = boto3.Session(profile_name='dev').get_credentials()
            self.assertEqual(credentials.method, 'custom-process')
            self.assertEqual(len(self.received), 1)
            type(self).reply['AccessKeyId'] = 'fixture-renewed-key'
            frozen = credentials.get_frozen_credentials()
        self.assertEqual((frozen.access_key, frozen.secret_key), ('fixture-renewed-key', 'fixture-private-secret'))
        self.assertEqual(len(self.received), 2)
        self.assertEqual(self.received[0], ('/api/sandbox-credentials/aws/test/aws', 'Bearer fixture-capability'))

    def test_revocation_and_auth_failure_emit_no_credentials(self):
        for status in (401, 503):
            type(self).status = status
            result = self.call_helper()
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, '')
            self.assertIn('AWS', result.stderr)
            for value in ('fixture-private-secret', 'fixture-capability', 'fixture-session-token'):
                self.assertNotIn(value, result.stderr)

    def test_bad_payload_and_expired_credentials_fail_closed(self):
        for payload in ({'Version': 1}, {**self.reply, 'Expiration': 'bad timestamp'},
                        {**self.reply, 'Expiration': '2000-01-01T00:00:00+00:00'}):
            type(self).reply = payload
            result = self.call_helper()
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, '')
            self.assertNotIn('fixture-private-secret', result.stderr)

    def test_plain_http_or_url_credentials_are_rejected_before_request(self):
        for url in (self.url.replace('https:', 'http:'), self.url.replace('localhost', 'user:private@localhost')):
            self.connection.write_text(json.dumps({'url': url, 'token': 'fixture-capability'}))
            result = self.call_helper()
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, '')
        self.assertEqual(self.received, [])

    def test_certificate_validation_is_required(self):
        self.env.pop('SSL_CERT_FILE')
        result = self.call_helper()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.received, [])

    def test_redirect_is_refused_without_forwarding_credentials(self):
        type(self).status = 302
        result = self.call_helper()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(len(self.received), 1)
        self.assertNotIn('fixture-capability', result.stderr)
