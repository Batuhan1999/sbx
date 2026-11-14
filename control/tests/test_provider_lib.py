"""The shared provider plumbing every catalog script relies on."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from catalog_scripts import CATALOG, LIB, ScriptCase, run_script, sbx_provider as sbx


class ResourceNamingTests(ScriptCase):
    def test_names_use_the_resource_id_with_one_separator(self):
        self.assertEqual(sbx.resource_name(), 'sbx-audit-resource')
        self.assertEqual(sbx.resource_name('_'), 'sbx_audit_resource')
        with self.assertRaises(sbx.ProviderError):
            sbx.resource_name('.')

    def test_invalid_resource_ids_are_refused(self):
        for value in ('', '../other', 'Upper', '1digit', 'a', 'x' * 32, 'with_underscore'):
            with self.subTest(value=value), patch.dict(os.environ, {'SBX_ID': value}):
                with self.assertRaisesRegex(sbx.ProviderError, 'resource ID'):
                    sbx.resource_name()

    def test_owned_names_follow_the_contract(self):
        self.assertTrue(sbx.owned('sbx-audit-resource'))
        self.assertTrue(sbx.owned('sbx_audit_resource', '_'))
        for name in ('sbx_audit', 'main', 'sbx-', 'other-sbx-x', None):
            with self.subTest(name=name):
                self.assertFalse(sbx.owned(name))

    def test_shell_scripts_get_the_same_name(self):
        result = run_script('_lib', 'sbx_provider.py', {'SBX_ID': 'audit-resource'}, 'name', '_')
        self.assertEqual((result.returncode, result.stdout), (0, 'sbx_audit_resource\n'), result.stderr)
        result = run_script('_lib', 'sbx_provider.py', {'SBX_ID': '../x'}, 'name')
        self.assertEqual(result.returncode, 1)
        self.assertIn('invalid service resource ID', result.stderr)

    def test_every_package_using_the_lib_carries_it(self):
        lib = (LIB / 'sbx_provider.py').read_text()
        users = {path.parent for path in CATALOG.glob('*/*') if path.is_file() and path.name != 'sbx_provider.py'
                 and not path.parent.name.startswith('_') and 'sbx_provider' in path.read_text()}
        self.assertIn(CATALOG / 'neon-branch', users)
        for folder in users:
            with self.subTest(package=folder.name):
                self.assertEqual((folder / 'sbx_provider.py').read_text(), lib)

    def test_scripts_run_from_a_copy_of_their_folder(self):
        """The runner copies a package's files elsewhere; its scripts must not need the catalog around them."""
        for folder in sorted(CATALOG.glob('*/provider.py')):
            with self.subTest(package=folder.parent.name), tempfile.TemporaryDirectory() as directory:
                copy = Path(shutil.copytree(folder.parent, Path(directory) / 'service'))
                result = subprocess.run([str(copy / 'provider.py'), 'list'], cwd=copy, capture_output=True, text=True,
                                        env={'PATH': os.environ['PATH'], 'SBX_ID': 'audit-resource', 'SBX_ACTION': 'list'})
                self.assertEqual(result.returncode, 1)
                self.assertRegex(result.stderr, r'^[^\n]+ is not set; connect the connection this package requires\n$')


class SavedStateTests(ScriptCase):
    def test_previous_output_must_be_an_object_with_string_identities(self):
        for previous in ('[]', '{invalid', '{"id": 42}', '{"id": ""}'):
            with self.subTest(previous=previous), patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': previous}):
                with self.assertRaisesRegex(sbx.ProviderError, 'invalid saved Fixture'):
                    sbx.previous('Fixture', 'id')
        with patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': '{"id": "kept", "other": 1}'}):
            self.assertEqual(sbx.previous('Fixture', 'id'), {'id': 'kept', 'other': 1})

    def test_recorded_resource_is_never_replaced_or_silently_recreated(self):
        with self.assertRaisesRegex(sbx.ProviderError, 'was replaced'):
            sbx.recorded('thing', 'new', 'saved')
        with self.assertRaisesRegex(sbx.ProviderError, 'is missing'):
            sbx.recorded('thing', None, 'saved', creating=True)
        sbx.recorded('thing', None, 'saved')
        sbx.recorded('thing', 'new', None, creating=True)

    def test_outputs_are_single_line_and_written_all_or_nothing(self):
        sbx.write_outputs({'url': 'postgresql://x', 'password': 'p=w'})
        self.assertEqual(self.values(), {'url': 'postgresql://x', 'password': 'p=w'})
        self.output.unlink()
        for values in ({'url': 'fine', 'password': 'two\nlines'}, {'bad-key': 'x'}, {'port': 5432}):
            with self.subTest(values=values), self.assertRaises(sbx.ProviderError):
                sbx.write_outputs(values)
            self.assertFalse(self.output.exists())


class ApiTests(unittest.TestCase):
    """sbx_provider.Api against a local HTTP server."""

    @classmethod
    def setUpClass(cls):
        cls.received, cls.answer = [], (200, b'{"ok": true}')

        class Handler(BaseHTTPRequestHandler):
            def answer(self):
                length = int(self.headers.get('Content-Length') or 0)
                cls.received.append((self.command, self.path, self.headers, self.rfile.read(length)))
                status, body = cls.answer
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = answer

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        type(self).answer = (200, b'{"ok": true}')
        self.received.clear()
        self.api = sbx.Api('Fixture', f'http://127.0.0.1:{self.server.server_address[1]}/v1',
                           {'Authorization': 'Bearer fixture-secret'})

    def test_json_requests_carry_auth_in_headers_only(self):
        self.assertEqual(self.api.request('/things', 'POST', {'name': 'x'}), {'ok': True})
        [(method, path, headers, body)] = self.received
        self.assertEqual((method, path), ('POST', '/v1/things'))
        self.assertEqual(json.loads(body), {'name': 'x'})
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(headers['User-Agent'], 'sbx')
        self.assertEqual(headers['Authorization'], 'Bearer fixture-secret')

    def test_errors_never_echo_credentials_or_response_bodies(self):
        type(self).answer = (403, b'fixture-secret')
        with self.assertRaisesRegex(sbx.HttpError, 'HTTP 403') as caught:
            self.api.request('/things')
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn('fixture-secret', str(caught.exception))
        with socket.socket() as unused:
            unused.bind(('127.0.0.1', 0))
            closed = unused.getsockname()[1]
        gone = sbx.Api('Fixture', f'http://fixture-secret@127.0.0.1:{closed}/v1', {'Authorization': 'Bearer fixture-secret'})
        with self.assertRaisesRegex(sbx.ProviderError, 'did not answer') as caught:
            gone.request('/things')
        self.assertNotIn('fixture-secret', str(caught.exception))

    def test_not_found_is_accepted_only_when_asked(self):
        type(self).answer = (404, b'')
        self.assertIsNone(self.api.request('/gone', 'DELETE', missing_ok=True))
        with self.assertRaisesRegex(sbx.ProviderError, 'HTTP 404'):
            self.api.request('/gone', 'DELETE')

    def test_non_json_response_is_refused(self):
        type(self).answer = (200, b'<html>')
        with self.assertRaisesRegex(sbx.ProviderError, 'not JSON'):
            self.api.request('/things')


class PollingTests(unittest.TestCase):
    def test_pagination_follows_tokens_and_refuses_a_repeat(self):
        pages = {None: ([1], 'b'), 'b': ([2], 'c'), 'c': ([3], None)}
        self.assertEqual(sbx.all_pages('Fixture', pages.__getitem__), [1, 2, 3])
        with self.assertRaisesRegex(sbx.ProviderError, 'repeated'):
            sbx.all_pages('Fixture', lambda token: ([], 'same'))
        with self.assertRaisesRegex(sbx.ProviderError, 'unexpected list'):
            sbx.all_pages('Fixture', lambda token: ({}, None))

    def test_wait_returns_the_first_ready_value_or_times_out(self):
        answers = iter([None, None, 'ready'])
        with patch('time.sleep') as sleep:
            self.assertEqual(sbx.wait('Fixture', lambda: next(answers), 60, interval=3), 'ready')
        self.assertEqual(sleep.call_count, 2)
        with patch('time.monotonic', side_effect=[0, 61]), self.assertRaisesRegex(sbx.ProviderError, 'Fixture is not ready'):
            sbx.wait('Fixture', lambda: None, 60)


class EntryPointTests(unittest.TestCase):
    def run_main(self, argv, run, actions=sbx.ACTIONS):
        with patch.object(sys, 'argv', ['provider.py', *argv]), self.assertRaises(SystemExit) as caught:
            sbx.main(run, actions)
            raise SystemExit(0)
        return caught.exception.code

    def test_actions_dispatch_and_failures_exit_with_a_safe_message(self):
        seen = []
        self.assertEqual(self.run_main(['delete'], seen.append), 0)
        self.assertEqual(self.run_main([], seen.append, ('create',)), 0)
        self.assertEqual(seen, ['delete', 'create'])
        self.assertIn('unsupported action', self.run_main(['destroy'], seen.append))
        def fails(action):
            raise sbx.ProviderError('fixture failure')
        self.assertEqual(self.run_main(['create'], fails), 'fixture failure')
        def incomplete(action):
            return {}['branches']
        self.assertIn('incomplete response', self.run_main(['create'], incomplete))


if __name__ == '__main__':
    unittest.main()
