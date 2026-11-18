"""Load catalog provider scripts as the runner runs them: from their package
folder, with the shared sbx_provider module importable."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

CATALOG = Path(__file__).resolve().parents[2] / 'catalog'
LIB = CATALOG / '_lib'
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
sbx_provider = importlib.import_module('sbx_provider')


def load(package, filename='provider.py'):
    path = CATALOG / package / filename
    spec = importlib.util.spec_from_file_location(f'catalog_{package}_{path.stem}'.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RedirectingServer:
    """A local HTTP server that answers every request with a 302 to /moved and
    records the paths it was asked for."""

    def __enter__(self):
        paths = self.paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/moved')
                self.send_header('Content-Length', '0')
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/'
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def run_script(package, script, env, *args):
    """Run one of a package's scripts from its folder, like the runner."""
    return subprocess.run([str(CATALOG / package / script), *args], cwd=CATALOG / package, env={**os.environ, **env},
                          capture_output=True, text=True)


class ScriptCase(unittest.TestCase):
    """A fresh $SBX_OUTPUT and the runner's environment for each test."""
    environment: dict = {}

    def setUp(self):
        self.output = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'output'
        self.enterContext(patch.dict(os.environ, {'SBX_ID': 'audit-resource', 'SBX_OUTPUT': str(self.output),
                                                  'SBX_PREVIOUS_OUTPUT': '{}', 'SBX_RESOURCE_STATUS': 'creating',
                                                  **self.environment}))

    def values(self):
        return dict(line.split('=', 1) for line in self.output.read_text().splitlines())


class Status(Exception):
    """A FakeApi handler's non-2xx answer."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class FakeApi:
    """A provider's HTTP API at the transport (sbx_provider.urlopen): handle()
    answers (method, path under base, decoded JSON body) with JSON, None for an
    empty reply, or raises Status. Every request is kept, headers included."""
    base = ''

    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        if not url.startswith(self.base):
            raise AssertionError(f'request outside the API: {url}')
        method, path = request.get_method(), url[len(self.base):]
        body = json.loads(request.data) if request.data else None
        self.requests.append({'method': method, 'url': url, 'path': path, 'body': body,
                              'headers': dict(request.header_items())})
        try:
            result = self.handle(method, path, body)
        except Status as status:
            raise HTTPError(url, status.code, 'fixture', {}, io.BytesIO(b'fixture-private-response')) from None
        return io.BytesIO(b'' if result is None else json.dumps(result).encode())

    def handle(self, method, path, body):
        raise NotImplementedError

    @property
    def mutations(self):
        return [(r['method'], r['path']) for r in self.requests if r['method'] != 'GET']

    def serve(self, case):
        """Answer the provider scripts' requests for the rest of the test."""
        case.enterContext(patch.object(sbx_provider, 'urlopen', side_effect=self))
        return self
