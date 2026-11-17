"""Supabase branch lifecycle contracts without cloud credentials."""
from contextlib import redirect_stdout
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import yaml

from catalog_scripts import CATALOG, FakeApi, ScriptCase, Status, load, sbx_provider

PROJECT = 'fixtureprojectrefaaa'
CHILD = 'fixturebranchrefaaaa'


class FakeSupabase(FakeApi):
    """The Management API for one project. A created branch is listed as
    setting up on the first read and as migrated from the next one on."""
    base = 'https://api.supabase.com/v1'

    def __init__(self, test, branches=(), keys=None):
        super().__init__()
        self.test = test
        self.branches = list(branches)
        self.password = 'fixture:@?# /'
        self.health = 'ACTIVE_HEALTHY'
        self.refuse_delete = False
        self.poolers = [{'database_type': 'PRIMARY', 'db_host': 'aws-0-fixture.pooler.supabase.com', 'db_port': 6543,
                         'db_user': f'postgres.{CHILD}', 'db_name': 'postgres', 'pool_mode': 'transaction'}]
        self.keys = keys if keys is not None else [
            {'name': 'anon', 'type': 'legacy', 'api_key': 'fixture-anon'},
            {'name': 'service_role', 'type': 'legacy', 'api_key': 'fixture-service'}]

    def handle(self, method, path, body):
        if path == f'/projects/{PROJECT}/branches':
            if method == 'POST':
                child = {'id': 'uuid-child', 'name': body['branch_name'], 'project_ref': CHILD, 'parent_project_ref': PROJECT,
                         'is_default': False, 'persistent': body['persistent'], 'with_data': body['with_data'],
                         'status': 'CREATING_PROJECT'}
                self.branches.append(child)
                return child
            return [dict(b) for b in self.branches]
        if path.startswith(f'/projects/{PROJECT}/branches/'):
            name = path.rsplit('/', 1)[1]
            branch = next((b for b in self.branches if b['name'] == name), None)
            if not branch:
                raise Status(404)
            shown = dict(branch)
            if branch.get('status') == 'CREATING_PROJECT':
                branch['status'] = 'MIGRATIONS_PASSED'
            return shown
        if path == f'/branches/{CHILD}':
            if method == 'DELETE':
                if self.refuse_delete:
                    raise Status(409)
                self.branches = [b for b in self.branches if b['project_ref'] != CHILD]
                return {'message': 'ok'}
            return {'ref': CHILD, 'status': self.health, 'db_host': f'db.{CHILD}.supabase.co', 'db_port': 5432,
                    'db_user': 'postgres', 'db_pass': self.password}
        if path == f'/projects/{CHILD}/config/database/pooler':
            return self.poolers
        if path == f'/projects/{CHILD}/api-keys?reveal=true':
            return self.keys
        if path.startswith('/branches/'):
            raise Status(404)
        self.test.fail(f'unexpected request: {method} {path}')


class SupabaseProviderTests(ScriptCase):
    environment = {'SUPABASE_ACCESS_TOKEN': 'fixture-private-token', 'SUPABASE_PROJECT': PROJECT,
                   'SBX_ID': 'fixture-resource', 'INPUT_WITH_DATA': 'false'}

    def setUp(self):
        super().setUp()
        self.enterContext(patch('time.sleep'))
        self.supabase = load('supabase-branch').Supabase()
        self.enterContext(patch.object(sbx_provider, 'urlopen', side_effect=lambda *a, **k: self.api(*a, **k)))
        self.main = {'id': 'uuid-main', 'name': 'main', 'project_ref': PROJECT, 'parent_project_ref': PROJECT,
                     'is_default': True, 'persistent': True, 'status': 'FUNCTIONS_DEPLOYED'}
        self.child = {'id': 'uuid-child', 'name': 'sbx-fixture-resource', 'project_ref': CHILD, 'parent_project_ref': PROJECT,
                      'is_default': False, 'persistent': False, 'status': 'FUNCTIONS_DEPLOYED'}

    def fake(self, **state):
        self.api = FakeSupabase(self, **state)
        return self.api

    def test_new_branch_is_created_once_waited_for_and_published(self):
        fake = self.fake(branches=[self.main])
        self.supabase.run('create')
        self.assertEqual(fake.mutations, [('POST', f'/projects/{PROJECT}/branches')])
        self.assertEqual(fake.requests[1]['body'], {'branch_name': 'sbx-fixture-resource', 'persistent': False, 'with_data': False})
        self.assertEqual(fake.requests[0]['headers']['Authorization'], 'Bearer fixture-private-token')
        values = self.values()
        url = urlsplit(values['url'])
        self.assertEqual((unquote(url.username), unquote(url.password), url.hostname, url.port, url.path),
                         (f'postgres.{CHILD}', 'fixture:@?# /', 'aws-0-fixture.pooler.supabase.com', 5432, '/postgres'))
        self.assertEqual((values['api_url'], values['anon_key'], values['service_role_key'], values['ref'], values['_branch_ref']),
                         (f'https://{CHILD}.supabase.co', 'fixture-anon', 'fixture-service', CHILD, CHILD))

    def test_with_data_is_passed_and_validated(self):
        fake = self.fake(branches=[self.main])
        with patch.dict(os.environ, {'INPUT_WITH_DATA': 'true'}):
            self.supabase.run('create')
        self.assertTrue(fake.branches[-1]['with_data'])
        fake = self.fake(branches=[self.main])
        with patch.dict(os.environ, {'INPUT_WITH_DATA': 'yes'}), self.assertRaisesRegex(ValueError, 'true or false'):
            self.supabase.run('create')
        self.assertEqual(fake.mutations, [])

    def test_branch_ref_is_recorded_before_waiting(self):
        fake = self.fake(branches=[self.main])
        fake.health = 'COMING_UP'
        clock = iter(range(0, 10_000, 60))
        with patch('time.monotonic', side_effect=lambda: next(clock)), self.assertRaisesRegex(ValueError, 'not ready; retry'):
            self.supabase.run('create')
        self.assertEqual(self.values(), {'_branch_ref': CHILD})

    def test_retry_resumes_the_recorded_branch_with_the_same_outputs(self):
        fake = self.fake(branches=[self.main])
        self.supabase.run('create')
        first = self.values()
        self.output.unlink()
        with patch.dict(os.environ, {'SBX_RESOURCE_STATUS': 'ready', 'SBX_PREVIOUS_OUTPUT': json.dumps(first)}):
            self.supabase.run('create')
        self.assertEqual(self.values(), first)
        self.assertEqual(fake.mutations, [('POST', f'/projects/{PROJECT}/branches')])

    def test_failed_setup_stops_without_publishing_connection_values(self):
        self.fake(branches=[self.main, {**self.child, 'status': 'MIGRATIONS_FAILED'}])
        with self.assertRaisesRegex(ValueError, 'MIGRATIONS_FAILED'):
            self.supabase.run('create')
        self.assertFalse(self.output.exists())

    def test_missing_password_or_keys_publish_nothing(self):
        self.fake(branches=[self.main, self.child]).password = None
        with self.assertRaisesRegex(ValueError, 'no database password'):
            self.supabase.run('create')
        self.fake(branches=[self.main, self.child], keys=[])
        with self.assertRaisesRegex(ValueError, 'no anon key'):
            self.supabase.run('create')
        self.assertFalse(self.output.exists())

    def test_new_api_keys_are_used_when_legacy_keys_are_off(self):
        self.fake(branches=[self.main, self.child], keys=[
            {'name': 'default', 'type': 'publishable', 'api_key': 'sb_publishable_fixture'},
            {'name': 'default', 'type': 'secret', 'api_key': 'sb_secret_fixture'}])
        self.supabase.run('create')
        self.assertEqual((self.values()['anon_key'], self.values()['service_role_key']),
                         ('sb_publishable_fixture', 'sb_secret_fixture'))

    def test_shared_pooler_is_chosen_over_a_dedicated_one(self):
        fake = self.fake(branches=[self.main, self.child])
        fake.poolers = [{'database_type': 'PRIMARY', 'db_host': f'db.{CHILD}.supabase.co', 'db_port': 6543,
                         'db_user': 'postgres', 'db_name': 'postgres'}] + fake.poolers
        self.supabase.run('create')
        self.assertEqual((self.values()['host'], self.values()['port']), ('aws-0-fixture.pooler.supabase.com', '5432'))
        fake = self.fake(branches=[self.main, self.child])
        fake.poolers = [{'database_type': 'PRIMARY', 'db_host': f'db.{CHILD}.supabase.co'}]
        self.output.unlink()
        with self.assertRaisesRegex(ValueError, 'no shared pooler'):
            self.supabase.run('create')
        self.assertFalse(self.output.exists())

    def test_listed_legacy_keys_without_values_fall_back_to_new_keys(self):
        self.fake(branches=[self.main, self.child], keys=[
            {'name': 'anon', 'type': 'legacy', 'api_key': None}, {'name': 'service_role', 'type': 'legacy', 'api_key': ''},
            {'name': 'default', 'type': 'publishable', 'api_key': 'sb_publishable_fixture'},
            {'name': 'default', 'type': 'secret', 'api_key': 'sb_secret_fixture'}])
        self.supabase.run('create')
        self.assertEqual((self.values()['anon_key'], self.values()['service_role_key']),
                         ('sb_publishable_fixture', 'sb_secret_fixture'))

    def test_delete_refused_during_setup_says_to_retry(self):
        fake = self.fake(branches=[self.main, {**self.child, 'status': 'RUNNING_MIGRATIONS'}])
        fake.refuse_delete = True
        with self.assertRaisesRegex(ValueError, 'still setting up'):
            self.supabase.run('delete')
        fake = self.fake(branches=[self.main, self.child])
        fake.refuse_delete = True
        with self.assertRaisesRegex(ValueError, 'HTTP 409'):
            self.supabase.run('delete')

    def test_branch_replaced_during_the_wait_is_not_used(self):
        fake = self.fake(branches=[self.main])
        original = fake.handle

        def replace(method, path, body):
            if path == f'/projects/{PROJECT}/branches/sbx-fixture-resource':
                fake.branches[-1]['project_ref'] = 'replacementrefaaaaaa'
            return original(method, path, body)
        fake.handle = replace
        with self.assertRaisesRegex(ValueError, 'recorded Supabase branch was replaced'):
            self.supabase.run('create')
        self.assertEqual(self.values(), {'_branch_ref': CHILD})

    def test_malformed_replies_stop_with_a_clear_error(self):
        for path, reply in ((f'/branches/{CHILD}', None), (f'/projects/{CHILD}/api-keys?reveal=true', ['key']),
                            (f'/projects/{CHILD}/config/database/pooler', {'database_type': 'PRIMARY'})):
            fake = self.fake(branches=[self.main, self.child])
            original = fake.handle
            fake.handle = lambda method, p, body, path=path, reply=reply, original=original: \
                reply if p == path and method == 'GET' else original(method, p, body)
            with self.subTest(path=path), self.assertRaisesRegex(sbx_provider.ProviderError, 'unexpected'):
                self.supabase.run('create')

    def test_same_name_replacement_is_never_reused_or_deleted(self):
        for action in ('create', 'delete'):
            fake = self.fake(branches=[self.main, {**self.child, 'project_ref': 'replacementrefaaaaaa'}])
            with self.subTest(action=action), patch.dict(os.environ, {
                    'SBX_PREVIOUS_OUTPUT': json.dumps({'_branch_ref': CHILD}), 'SBX_RESOURCE_STATUS': 'ready'}):
                with self.assertRaisesRegex(ValueError, 'recorded Supabase branch was replaced'):
                    self.supabase.run(action)
                self.assertEqual(fake.mutations, [])

    def test_missing_recorded_branch_is_not_recreated_and_delete_is_idempotent(self):
        fake = self.fake(branches=[self.main])
        with patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': json.dumps({'_branch_ref': CHILD})}):
            with self.assertRaisesRegex(ValueError, 'recorded Supabase branch is missing'):
                self.supabase.run('create')
            self.supabase.run('delete')
        self.assertEqual(fake.mutations, [])

    def test_delete_removes_only_the_owned_branch(self):
        fake = self.fake(branches=[self.main, self.child])
        self.supabase.run('delete')
        self.supabase.run('delete')
        self.assertEqual(fake.mutations, [('DELETE', f'/branches/{CHILD}')])

    def test_default_persistent_or_foreign_branches_are_never_modified(self):
        for action in ('create', 'delete'):
            for change, message in (({'is_default': True}, 'default or persistent'), ({'persistent': True}, 'default or persistent'),
                                    ({'project_ref': PROJECT}, 'default or persistent'),
                                    ({'parent_project_ref': 'otherprojectrefaaaaa'}, 'another project')):
                fake = self.fake(branches=[{**self.child, **change}])
                with self.subTest(action=action, change=change), self.assertRaisesRegex(ValueError, message):
                    self.supabase.run(action)
                self.assertEqual(fake.mutations, [])

    def test_branch_scheduled_for_deletion_is_not_reused(self):
        self.fake(branches=[{**self.child, 'deletion_scheduled_at': '2026-11-17T10:00:00Z'}])
        with self.assertRaisesRegex(ValueError, 'scheduled for deletion'):
            self.supabase.run('create')

    def test_list_prints_only_owned_branch_names(self):
        self.fake(branches=[self.main, self.child, {**self.child, 'name': 'feature-x'}])
        output = io.StringIO()
        with redirect_stdout(output):
            self.supabase.run('list')
        self.assertEqual(output.getvalue(), 'sbx-fixture-resource\n')

    def test_errors_never_carry_the_token_or_response(self):
        fake = self.fake(branches=[self.main])
        listing = fake.handle
        fake.handle = lambda method, path, body: (_ for _ in ()).throw(Status(402)) if method == 'POST' \
            else listing(method, path, body)
        with self.assertRaises(ValueError) as caught:
            self.supabase.run('create')
        self.assertIn('HTTP 402', str(caught.exception))
        self.assertIn('paid plan', str(caught.exception))
        self.assertNotIn('fixture-private', str(caught.exception))


class SupabaseProjectCheckTests(unittest.TestCase):
    def run_check(self, project, answers):
        check = yaml.safe_load((CATALOG/'supabase-project/package.yaml').read_text())['check']
        script = '\n'.join(check.splitlines()[1:-1])
        seen = []

        def urlopen(request, **kwargs):
            seen.append(request.full_url)
            return io.BytesIO(json.dumps(answers[len(seen) - 1]).encode())
        with patch.dict(os.environ, {'INPUT_PROJECT': project, 'INPUT_ACCESS_TOKEN': 'fixture-private-token'}), \
                patch('urllib.request.urlopen', side_effect=urlopen):
            exec(compile(script, '<Supabase connection check>', 'exec'), {})
        return seen

    def test_check_quotes_the_ref_and_reads_the_branches(self):
        seen = self.run_check('ref/x?y', [{'ref': 'ref/x?y'}, []])
        self.assertEqual(seen, ['https://api.supabase.com/v1/projects/ref%2Fx%3Fy',
                                'https://api.supabase.com/v1/projects/ref%2Fx%3Fy/branches'])

    def test_check_rejects_another_project_without_echoing_the_token(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_check('wanted', [{'ref': 'other'}])
        self.assertNotIn('fixture-private-token', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
