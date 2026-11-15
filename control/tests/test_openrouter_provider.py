"""OpenRouter key lifecycle and account check against a fake key API, without an account."""
from contextlib import redirect_stdout
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import yaml

from catalog_scripts import CATALOG, FakeApi, ScriptCase, Status, load

NAME = 'sbx-audit-resource'


def stored(key_hash, name=NAME, **changes):
    return {'hash': key_hash, 'name': name, 'label': 'sk-or-v1-abc...xyz', 'disabled': False, 'limit': 5,
            'limit_remaining': 5, 'limit_reset': None, 'include_byok_in_limit': True, 'usage': 0, **changes}


class FakeOpenRouter(FakeApi):
    """The workspace's keys, listed page_size at a time; read_only refuses
    every write the way a read-only management key is refused."""
    base = 'https://openrouter.ai/api/v1/keys'

    def __init__(self, *keys, page_size=100):
        super().__init__()
        self.keys = {k['hash']: k for k in keys}
        self.page_size, self.read_only, self.listed, self.ignore_patch, self.created = page_size, False, None, False, []

    def handle(self, method, path, body):
        if method != 'GET' and self.read_only:
            raise Status(403)
        if path.startswith('?'):
            query = parse_qs(path[1:])
            assert query['include_disabled'] == ['true'], 'disabled keys must be listed too'
            offset = int(query['offset'][0])
            return {'data': list(self.keys.values())[offset:offset + self.page_size] if self.listed is None else self.listed}
        if (method, path) == ('POST', ''):
            self.created.append(body)
            made = stored(f'hash-{len(self.created)}', body['name'], limit=body['limit'], limit_reset=body['limit_reset'],
                          include_byok_in_limit=body['include_byok_in_limit'])
            self.keys[made['hash']] = made
            return {'data': made, 'key': f'sk-or-v1-fixture-{len(self.created)}'}
        key_hash = path.removeprefix('/')
        if key_hash not in self.keys:
            raise Status(404)
        if method == 'DELETE':
            del self.keys[key_hash]
            return {'deleted': True}
        if method == 'PATCH' and not self.ignore_patch:
            self.keys[key_hash].update(body)
        return {'data': self.keys[key_hash]}


class OpenRouterKeyTests(ScriptCase):
    environment = {'OPENROUTER_MANAGEMENT_KEY': 'fixture-management-key', 'INPUT_LIMIT': '5',
                   'INPUT_LIMIT_RESET': '', 'INPUT_INCLUDE_BYOK_IN_LIMIT': 'true'}

    def setUp(self):
        super().setUp()
        self.provider = load('openrouter-key').OpenRouter()

    def openrouter(self, *keys, **options):
        return FakeOpenRouter(*keys, **options).serve(self)

    def previous(self, value):
        return patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': value if isinstance(value, str) else json.dumps(value)})

    def inputs(self, **values):
        return patch.dict(os.environ, {f'INPUT_{k.upper()}': v for k, v in values.items()})

    def test_create_makes_a_named_key_with_an_explicit_limit_and_publishes_it(self):
        openrouter = self.openrouter(stored('other', 'someone-else'))
        self.provider.run('create')
        self.assertEqual(openrouter.created, [{'name': NAME, 'limit': 5.0, 'limit_reset': None,
                                               'include_byok_in_limit': True}])
        self.assertEqual(self.values(), {'key': 'sk-or-v1-fixture-1', 'hash': 'hash-1',
                                         'base_url': 'https://openrouter.ai/api/v1',
                                         'anthropic_base_url': 'https://openrouter.ai/api'})
        self.assertEqual(list(openrouter.keys), ['other', 'hash-1'])

    def test_limit_settings_are_sent_as_given(self):
        openrouter = self.openrouter()
        with self.inputs(limit='12.5', limit_reset='Weekly', include_byok_in_limit='false'):
            self.provider.run('create')
        self.assertEqual(openrouter.created, [{'name': NAME, 'limit': 12.5, 'limit_reset': 'weekly',
                                               'include_byok_in_limit': False}])

    def test_requests_use_the_management_key_as_a_bearer_and_never_in_the_url(self):
        openrouter = self.openrouter()
        self.provider.run('create')
        for request in openrouter.requests:
            self.assertEqual(request['headers']['Authorization'], 'Bearer fixture-management-key')
            self.assertEqual(request['headers']['User-agent'], 'sbx')
            self.assertNotIn('fixture-management-key', request['url'])

    def test_retry_reuses_the_saved_key_without_changing_anything(self):
        openrouter = self.openrouter()
        self.provider.run('create')
        first = self.values()
        self.output.unlink()
        with self.previous(first), patch.dict(os.environ, {'SBX_RESOURCE_STATUS': 'ready'}):
            self.provider.run('create')
        self.assertEqual(self.values(), first)
        self.assertEqual(openrouter.mutations, [('POST', '')])

    def test_changed_limit_settings_update_the_same_key(self):
        openrouter = self.openrouter(stored('hash-1'))
        with self.previous({'key': 'sk-or-v1-saved', 'hash': 'hash-1'}), \
                self.inputs(limit='20', limit_reset='monthly', include_byok_in_limit='false'):
            self.provider.run('create')
        self.assertEqual(openrouter.mutations, [('PATCH', '/hash-1')])
        self.assertEqual(openrouter.requests[-1]['body'], {'limit': 20.0, 'limit_reset': 'monthly',
                                                           'include_byok_in_limit': False})
        self.assertEqual((self.values()['key'], self.values()['hash']), ('sk-or-v1-saved', 'hash-1'))

    def test_settings_openrouter_did_not_apply_are_an_error(self):
        openrouter = self.openrouter(stored('hash-1'))
        openrouter.ignore_patch = True
        with self.previous({'key': 'sk-or-v1-saved', 'hash': 'hash-1'}), self.inputs(limit='20'), \
                self.assertRaisesRegex(ValueError, 'did not apply'):
            self.provider.run('create')
        self.assertNotIn('base_url', self.values())

    def test_an_unsaved_key_left_by_a_stopped_create_is_replaced(self):
        openrouter = self.openrouter(stored('orphan'), stored('orphan-2'), stored('other', 'someone-else'))
        self.provider.run('create')
        self.assertEqual(openrouter.mutations, [('DELETE', '/orphan'), ('DELETE', '/orphan-2'), ('POST', '')])
        self.assertEqual(sorted(openrouter.keys), ['hash-1', 'other'])
        self.assertEqual(self.values()['hash'], 'hash-1')

    def test_a_recorded_key_that_is_gone_or_renamed_is_not_replaced(self):
        for keys, error in (((), 'recover it explicitly'), ((stored('hash-1', 'renamed'),), 'renamed')):
            openrouter = self.openrouter(*keys)
            with self.subTest(error=error), self.previous({'key': 'sk-or-v1-saved', 'hash': 'hash-1'}), \
                    self.assertRaisesRegex(ValueError, error):
                self.provider.run('create')
            self.assertEqual(openrouter.mutations, [])
            self.assertFalse(self.output.exists())

    def test_invalid_inputs_or_saved_state_make_no_request(self):
        openrouter = self.openrouter()
        for inputs in ({'limit': ''}, {'limit': '0'}, {'limit': '-1'}, {'limit': 'five'}, {'limit': 'nan'},
                       {'limit': 'inf'}, {'limit': '1000000'}, {'limit_reset': 'yearly'},
                       {'include_byok_in_limit': 'maybe'}):
            with self.subTest(inputs=inputs), self.inputs(**inputs), self.assertRaises(ValueError):
                self.provider.run('create')
        for previous in ('invalid-json', '[]', '{"hash": 7, "key": "k"}', '{"hash": "h"}', '{"key": "k"}'):
            for action in ('create', 'delete'):
                with self.subTest(previous=previous, action=action), self.previous(previous), \
                        self.assertRaisesRegex(ValueError, 'invalid saved OpenRouter'):
                    self.provider.run(action)
        with patch.dict(os.environ, {'SBX_ID': '../other'}), self.assertRaisesRegex(ValueError, 'resource ID'):
            self.provider.run('delete')
        self.assertEqual(openrouter.requests, [])
        self.assertFalse(self.output.exists())

    def test_a_read_only_management_key_publishes_nothing_and_errors_name_no_secret(self):
        openrouter = self.openrouter()
        openrouter.read_only = True
        with self.assertRaisesRegex(ValueError, r'HTTP 403\); check that the management key is read/write') as caught:
            self.provider.run('create')
        self.assertNotIn('fixture', str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_delete_removes_only_the_recorded_key_and_is_idempotent(self):
        openrouter = self.openrouter(stored('hash-1'), stored('replacement'))
        with self.previous({'key': 'sk-or-v1-saved', 'hash': 'hash-1'}):
            self.provider.run('delete')
            self.provider.run('delete')
        self.assertEqual(list(openrouter.keys), ['replacement'])
        self.assertEqual(openrouter.mutations, [('DELETE', '/hash-1'), ('DELETE', '/hash-1')])

    def test_delete_without_a_record_removes_only_keys_named_for_the_resource(self):
        openrouter = self.openrouter(stored('orphan'), stored('other', 'sbx-another-resource'))
        self.provider.run('delete')
        self.provider.run('delete')
        self.assertEqual(list(openrouter.keys), ['other'])

    def test_list_follows_every_page_and_prints_only_sbx_names(self):
        keys = [stored(f'k{i}', f'sbx-resource-{i}', disabled=i == 1) for i in range(5)]
        self.openrouter(*keys, stored('mine', 'personal laptop'), page_size=2)
        output = io.StringIO()
        with redirect_stdout(output):
            self.provider.run('list')
        self.assertEqual(output.getvalue().split(), [f'sbx-resource-{i}' for i in range(5)])

    def test_a_list_that_is_not_an_array_is_refused_before_any_change(self):
        openrouter = self.openrouter(stored('orphan'))
        openrouter.listed = {'error': 'fixture'}
        with self.assertRaisesRegex(ValueError, 'unexpected list'):
            self.provider.run('delete')
        self.assertEqual(openrouter.mutations, [])


class OpenRouterAccountCheckTests(unittest.TestCase):
    """The connection's check, run as the platform runs its heredoc."""
    check = yaml.safe_load((CATALOG / 'openrouter-account/package.yaml').read_text())['check']
    script = '\n'.join(check.splitlines()[1:-1])

    def run_check(self, answers):
        seen = []

        def urlopen(request, timeout=None):
            seen.append((request.get_method(), urlsplit(request.full_url).path, request.headers,
                         json.loads(request.data) if request.data else None))
            code, body = answers[len(seen) - 1]
            if code is None:
                raise TimeoutError('timed out')
            if code >= 400:
                raise HTTPError(request.full_url, code, 'fixture', {}, io.BytesIO(b'fixture-private-response'))
            response = io.BytesIO(json.dumps(body).encode())
            response.status = code
            return response
        with patch.dict(os.environ, {'INPUT_MANAGEMENT_KEY': 'fixture-management-key'}), \
                patch('urllib.request.urlopen', side_effect=urlopen):
            try:
                exec(compile(self.script, '<OpenRouter account check>', 'exec'), {})
            except SystemExit as error:
                self.assertNotIn('fixture', str(error))
                return str(error), seen
        return '', seen

    def test_a_read_write_key_passes_without_changing_anything(self):
        why, seen = self.run_check([(200, {'data': []}), (404, None)])
        self.assertEqual(why, '')
        self.assertEqual([(m, p, b) for m, p, _, b in seen], [
            ('GET', '/api/v1/keys', None), ('PATCH', '/api/v1/keys/' + '0' * 64, {'disabled': False})])
        self.assertEqual(seen[0][2]['Authorization'], 'Bearer fixture-management-key')

    def test_read_only_wrong_or_expired_keys_say_which(self):
        for answers, expected in (([(200, {'data': []}), (403, None)], 'read-only'),
                                  ([(200, {'data': []}), (401, None)], 'read-only'),
                                  ([(401, None)], 'not an inference API key'),
                                  ([(200, {'error': 'fixture'})], 'could not list'),
                                  ([(500, None)], 'HTTP 500'),
                                  ([(None, None)], 'did not answer')):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.run_check(answers)[0])

    def test_an_inconclusive_write_probe_fails_and_asks_for_a_retry(self):
        for code in (200, 400, 429, 500, 503):
            with self.subTest(code=code):
                why, _ = self.run_check([(200, {'data': []}), (code, {'data': {}})])
                self.assertIn(f'(HTTP {code}); try the check again', why)
        self.assertIn('did not answer', self.run_check([(200, {'data': []}), (None, None)])[0])


if __name__ == '__main__':
    unittest.main()
