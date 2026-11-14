"""Upstash provider contracts against a fake Upstash API, without a hosted account."""
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from catalog_scripts import FakeApi, ScriptCase, Status, load


def ready(**changes):
    return {'database_id': 'db-fixture', 'database_name': 'sbx-audit-resource', 'state': 'active',
            'endpoint': 'development-fixture', 'port': 6379, 'password': 'fixture:@?# /',
            'rest_token': 'fixture-rest-token', 'tls': True, 'primary_region': 'eu-central-1', **changes}


class FakeUpstash(FakeApi):
    """The account's databases. listed overrides the list reply; detail changes
    what a database's own page says; pending is how many reads of a new or
    existing database still find it starting."""
    base = 'https://api.upstash.com/v2'

    def __init__(self, *databases):
        super().__init__()
        self.databases = {d['database_id']: d for d in databases}
        self.listed, self.detail, self.pending, self.created = None, {}, 0, []

    def handle(self, method, path, body):
        if (method, path) == ('GET', '/redis/databases'):
            return list(self.databases.values()) if self.listed is None else self.listed
        if (method, path) == ('POST', '/redis/database'):
            self.created.append(body)
            self.databases['db-fixture'] = ready(state='creating', database_name=body['database_name'],
                                                 primary_region=body['primary_region'])
            return {'database_id': 'db-fixture'}
        identifier = path.removeprefix('/redis/database/')
        if identifier not in self.databases:
            raise Status(404)
        if method == 'DELETE':
            del self.databases[identifier]
            return None
        if self.pending:
            self.pending -= 1
            return {**self.databases[identifier], 'state': 'passive'}
        self.databases[identifier]['state'] = 'active' if self.databases[identifier]['state'] == 'creating' else \
            self.databases[identifier]['state']
        return {**self.databases[identifier], **self.detail}


class UpstashProviderTests(ScriptCase):
    environment = {'UPSTASH_EMAIL': 'fixture@example.test', 'UPSTASH_API_KEY': 'fixture-api-key',
                   'INPUT_REGION': 'eu-central-1', 'INPUT_PLATFORM': 'aws', 'INPUT_PLAN': 'free', 'INPUT_BUDGET': '1'}

    def setUp(self):
        super().setUp()
        self.enterContext(patch('time.sleep'))
        self.module = load('upstash-redis')
        self.api = self.module.Upstash()

    def upstash(self, *databases):
        return FakeUpstash(*databases).serve(self)

    def previous(self, value):
        return patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': value if isinstance(value, str) else json.dumps(value)})

    def test_creation_asks_for_the_explicit_plan_region_budget_and_tls(self):
        upstash = self.upstash()
        self.api.run('create')
        self.assertEqual(upstash.created, [{'database_name': 'sbx-audit-resource', 'platform': 'aws',
            'primary_region': 'eu-central-1', 'plan': 'free', 'budget': 1, 'tls': True, 'eviction': False}])
        values = self.values()
        parsed = urlsplit(values['url'])
        self.assertEqual((parsed.scheme, parsed.username, parsed.hostname, parsed.port, parsed.path),
                         ('rediss', 'default', 'development-fixture.upstash.io', 6379, '/0'))
        self.assertEqual(unquote(parsed.password), 'fixture:@?# /')
        self.assertEqual(values['rest_url'], 'https://development-fixture.upstash.io')
        self.assertEqual(values['rest_token'], 'fixture-rest-token')

    def test_requests_identify_sbx_and_keep_auth_out_of_the_url(self):
        upstash = self.upstash()
        self.api.run('create')
        for request in upstash.requests:
            self.assertEqual(request['headers']['User-agent'], 'sbx')
            self.assertTrue(request['headers']['Authorization'].startswith('Basic '))
            self.assertNotIn('fixture-api-key', request['url'])
            self.assertNotIn('fixture@example.test', request['url'])

    def test_retry_reuses_the_same_database_without_recreating_or_resetting(self):
        upstash = self.upstash(ready())
        self.api.run('create')
        first = self.values()
        self.output.unlink()
        self.api.run('create')
        self.assertEqual(first, self.values())
        self.assertEqual(upstash.mutations, [])

    def test_a_starting_database_is_polled_instead_of_recreated(self):
        upstash = self.upstash(ready())
        upstash.pending = 2
        self.api.run('create')
        self.assertTrue(self.output.exists())
        self.assertEqual(upstash.mutations, [])

    def test_a_pending_timeout_leaves_the_database_for_the_same_resource_to_retry(self):
        upstash = self.upstash(ready())
        upstash.pending = 100
        with patch('time.monotonic', side_effect=[0, 91]), self.assertRaisesRegex(ValueError, 'retry the same service'):
            self.api.run('create')
        self.assertFalse(self.output.exists())
        self.assertEqual((upstash.mutations, list(upstash.databases)), ([], ['db-fixture']))

    def test_saved_identity_refuses_a_same_name_replacement_on_create_and_delete(self):
        for action in ('create', 'delete'):
            upstash = self.upstash(ready())
            with self.subTest(action=action), self.previous({'database_id': 'recorded-id'}), \
                    patch.dict(os.environ, {'SBX_RESOURCE_STATUS': 'ready'}):
                with self.assertRaisesRegex(ValueError, 'database was replaced'):
                    self.api.run(action)
                self.assertEqual(upstash.mutations, [])
                self.assertFalse(self.output.exists())

    def test_a_missing_saved_database_is_not_recreated_and_its_delete_is_idempotent(self):
        upstash = self.upstash()
        with self.previous({'database_id': 'recorded-id'}):
            with self.assertRaisesRegex(ValueError, 'recover it explicitly'):
                self.api.run('create')
            self.api.run('delete')
        self.assertFalse(self.output.exists())
        self.assertEqual(upstash.mutations, [])

    def test_saved_identity_is_reused_for_create_and_delete(self):
        upstash = self.upstash(ready())
        with self.previous({'database_id': 'db-fixture'}):
            self.api.run('create')
            self.assertEqual(self.values()['database_id'], 'db-fixture')
            self.api.run('delete')
        self.assertEqual(upstash.databases, {})
        self.assertEqual(upstash.created, [])

    def test_invalid_saved_identity_or_resource_id_fails_before_any_api_call(self):
        upstash = self.upstash(ready())
        for previous in ('invalid-json', '[]', '{"database_id": 17}', '{"database_id": ""}'):
            with self.subTest(previous=previous), self.previous(previous):
                with self.assertRaisesRegex(ValueError, 'invalid saved Upstash'):
                    self.api.run('create')
        with patch.dict(os.environ, {'SBX_ID': '../other-database'}), self.assertRaisesRegex(ValueError, 'resource ID'):
            self.api.run('delete')
        self.assertEqual(upstash.requests, [])

    def test_unsafe_existing_databases_publish_no_credentials_and_are_not_changed(self):
        for detail, error in (({'database_id': 'replacement-id'}, 'identity changed'),
                              ({'state': 'suspended'}, 'suspended'),
                              ({'password': None}, 'withheld'), ({'rest_token': None}, 'withheld'),
                              ({'primary_region': 'us-east-1'}, 'different region')):
            with self.subTest(detail=detail):
                upstash = self.upstash(ready())
                upstash.detail = detail
                with self.assertRaisesRegex(ValueError, error):
                    self.api.run('create')
                self.assertFalse(self.output.exists())
                self.assertEqual(upstash.mutations, [])

    def test_ambiguous_names_are_never_deleted(self):
        upstash = self.upstash(ready(), ready(database_id='another-id'))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.api.run('delete')
        self.assertEqual(len(upstash.databases), 2)

    def test_delete_removes_only_the_exact_resource_and_is_idempotent(self):
        upstash = self.upstash(ready(database_id='other-id', database_name='other-database'), ready())
        self.api.run('delete')
        self.api.run('delete')
        self.assertEqual(list(upstash.databases), ['other-id'])

    def test_delete_accepts_a_database_gone_since_it_was_listed(self):
        upstash = self.upstash()
        upstash.listed = [ready()]
        self.api.run('delete')
        self.assertEqual(upstash.mutations, [('DELETE', '/redis/database/db-fixture')])

    def test_a_non_array_database_list_is_refused_before_any_change(self):
        upstash = self.upstash(ready())
        upstash.listed = {'error': 'fixture failure'}
        with self.assertRaisesRegex(ValueError, 'unexpected database list'):
            self.api.run('delete')
        self.assertEqual(upstash.mutations, [])

    def test_unsupported_region_budget_or_plan_never_creates_a_database(self):
        for values in ({'INPUT_PLATFORM': 'gcp'}, {'INPUT_BUDGET': '0'}, {'INPUT_BUDGET': '-1'},
                       {'INPUT_BUDGET': '1.5'}, {'INPUT_PLAN': 'unexpected'}):
            upstash = self.upstash()
            with self.subTest(values=values), patch.dict(os.environ, values):
                with self.assertRaisesRegex(ValueError, 'supported Upstash plan|matching primary region'):
                    self.api.run('create')
                self.assertEqual(upstash.mutations, [])

    def test_ready_connection_rejects_plaintext_tls_and_malformed_values(self):
        for values in ({'tls': False}, {'endpoint': 'https://unsafe/host'}, {'endpoint': 'bad..host'},
                       {'port': 'not-a-port'}, {'port': 0}, {'port': None}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.module.connection(ready(**values))
        result = self.module.connection(ready(endpoint='already-full.upstash.io'))
        self.assertEqual(result['host'], 'already-full.upstash.io')


if __name__ == '__main__':
    unittest.main()
