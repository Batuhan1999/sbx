"""Neon discovery and branch lifecycle contracts without cloud credentials."""
from contextlib import redirect_stdout
import io
import json
import os
import re
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import yaml

from catalog_scripts import CATALOG, FakeApi, ScriptCase, load, sbx_provider

READY = {'type': 'read_write', 'host': 'fixture.neon.test', 'current_state': 'idle'}


class FakeNeon(FakeApi):
    """The project API: branches with their databases, roles and endpoints. A
    branch created with a read_write endpoint gets one that is starting, and
    idle (connectable) from the next read on."""
    base = 'https://console.neon.tech/api/v2/projects/fixture-project'

    def __init__(self, test, branches=(), databases=(), roles=(), endpoints=None):
        super().__init__()
        self.test = test
        self.branches, self.databases, self.roles = list(branches), list(databases), list(roles)
        self.endpoints = endpoints or {}
        self.pages, self.password = {}, 'fixture:@?# /'

    def handle(self, method, path, body):
        if path.startswith('/branches?'):
            cursor = re.search(r'cursor=([^&]+)', path)
            return self.pages[cursor and cursor[1]] if self.pages else {'branches': self.branches}
        if (method, path) == ('POST', '/branches'):
            child = {'id': 'br-child', 'name': body['branch']['name'], 'parent_id': body['branch']['parent_id']}
            self.branches.append(child)
            if {'type': 'read_write'} in body.get('endpoints', []):
                self.endpoints['br-child'] = [{**READY, 'host': None, 'current_state': 'init'}]
            return {'branch': child}
        if (method, path) == ('POST', '/endpoints'):
            self.endpoints[body['endpoint']['branch_id']] = [READY]
            return {'endpoint': {'id': 'ep-new'}}
        found = re.fullmatch(r'/branches/([^/]+)(?:/(databases|roles|endpoints))?(?:/([^/]+)/reveal_password)?', path)
        self.test.assertIsNotNone(found, f'unexpected request: {method} {path}')
        branch, collection, role = found.groups()
        if method == 'DELETE':
            self.branches = [b for b in self.branches if b['id'] != branch]
            return None
        if role:
            return {'password': self.password}
        if collection == 'endpoints':
            found = self.endpoints.get(branch, [])
            self.endpoints[branch] = [READY if e.get('current_state') == 'init' else e for e in found]
            return {'endpoints': found}
        return {collection: {'databases': self.databases, 'roles': self.roles}[collection]}


class NeonProviderTests(ScriptCase):
    environment = {'NEON_API_KEY': 'fixture-private-key', 'NEON_PROJECT': 'fixture-project',
                   'SBX_ID': 'fixture-resource', 'INPUT_PARENT': '', 'INPUT_DATABASE': '', 'INPUT_ROLE': ''}

    def setUp(self):
        super().setUp()
        self.enterContext(patch('time.sleep'))
        self.neon = load('neon-branch').Neon()
        self.enterContext(patch.object(sbx_provider, 'urlopen', side_effect=lambda *a, **k: self.neon_api(*a, **k)))
        self.parent = {'id': 'br-parent', 'name': 'production', 'default': True}
        self.child = {'id': 'br-child', 'name': 'sbx-fixture-resource'}

    def fake(self, **state):
        self.neon_api = FakeNeon(self, **state)
        return self.neon_api

    def test_blank_parent_discovers_actual_project_default(self):
        self.fake(branches=[{'id': 'br-other', 'name': 'development'}, self.parent])
        self.assertEqual(self.neon.parent()['id'], 'br-parent')

    def test_current_default_wins_over_deprecated_primary_flag(self):
        self.fake(branches=[{'id': 'br-old', 'name': 'old-main', 'primary': True}, self.parent])
        self.assertEqual(self.neon.parent()['id'], 'br-parent')

    def test_ambiguous_default_requires_parent_selection(self):
        self.fake(branches=[{'id': 'one', 'name': 'one', 'default': True}, {'id': 'two', 'name': 'two', 'default': True}])
        with self.assertRaisesRegex(ValueError, 'select a parent'):
            self.neon.parent()

    def test_explicit_parent_accepts_name_or_id_and_never_falls_back(self):
        self.fake(branches=[self.parent, {'id': 'br-dev', 'name': 'development'}])
        for selected in ('development', 'br-dev'):
            with self.subTest(selected=selected), patch.dict(os.environ, {'INPUT_PARENT': selected}):
                self.assertEqual(self.neon.parent()['id'], 'br-dev')
        with patch.dict(os.environ, {'INPUT_PARENT': 'missing'}), self.assertRaisesRegex(ValueError, 'does not exist'):
            self.neon.parent()

    def test_branch_list_follows_pagination_and_refuses_a_repeated_cursor(self):
        fake = self.fake()
        fake.pages = {None: {'branches': [{'id': 'other', 'name': 'other'}], 'pagination': {'next': 'next-page'}},
                      'next-page': {'branches': [{'id': 'br-main', 'name': 'main'}]}}
        self.assertEqual(self.neon.matching('main')['id'], 'br-main')
        fake.pages = {None: {'branches': [], 'pagination': {'next': 'same'}}, 'same': {'branches': [], 'pagination': {'next': 'same'}}}
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'repeated'):
            self.neon.run('list')

    def test_database_and_owning_login_are_discovered(self):
        self.fake(databases=[{'name': 'postgres', 'owner_name': 'admin'}, {'name': 'application', 'owner_name': 'application_owner'}],
                  roles=[{'name': 'application_owner', 'authentication_method': 'password'}])
        self.assertEqual(self.neon.connection_selection({'id': 'br-parent'}), ('application_owner', 'application'))

    def test_multiple_application_databases_require_selection(self):
        self.fake(databases=[{'name': 'one', 'owner_name': 'owner'}, {'name': 'two', 'owner_name': 'owner'}])
        with self.assertRaisesRegex(ValueError, 'select a Neon database explicitly'):
            self.neon.connection_selection({'id': 'br-parent'})

    def test_selected_database_discovers_its_owner(self):
        self.fake(databases=[{'name': 'other', 'owner_name': 'other_owner'}, {'name': 'selected', 'owner_name': 'selected_owner'}],
                  roles=[{'name': 'selected_owner'}])
        with patch.dict(os.environ, {'INPUT_DATABASE': 'selected'}):
            self.assertEqual(self.neon.connection_selection({'id': 'br-parent'}), ('selected_owner', 'selected'))

    def test_nonlogin_owner_requires_explicit_migration_role(self):
        self.fake(databases=[{'name': 'application', 'owner_name': 'group_owner'}],
                  roles=[{'name': 'group_owner', 'authentication_method': 'no_login'}])
        with self.assertRaisesRegex(ValueError, 'migration-capable role'):
            self.neon.connection_selection({'id': 'br-parent'})

    def test_explicit_selectors_override_saved_connection_without_discovery(self):
        fake = self.fake()
        with patch.dict(os.environ, {'INPUT_DATABASE': 'chosen-db', 'INPUT_ROLE': 'chosen-role'}):
            self.assertEqual(self.neon.connection_selection({'id': 'br-child'}, {'database': 'old-db', 'user': 'old-user'}),
                             ('chosen-role', 'chosen-db'))
        self.assertEqual(fake.requests, [])

    def test_saved_connection_is_validated_against_child_metadata(self):
        previous = {'database': 'application', 'user': 'application_owner'}
        self.fake(databases=[{'name': 'different', 'owner_name': 'other'}])
        with self.assertRaisesRegex(ValueError, 'select a Neon database'):
            self.neon.connection_selection({'id': 'br-child'}, previous)
        self.fake(databases=[{'name': 'application', 'owner_name': 'other'}], roles=[{'name': 'other'}])
        with self.assertRaisesRegex(ValueError, 'migration-capable role'):
            self.neon.connection_selection({'id': 'br-child'}, previous)

    def test_new_branch_is_created_once_from_the_parent_and_published(self):
        fake = self.fake(branches=[self.parent], databases=[{'name': 'application', 'owner_name': 'application_owner'}],
                         roles=[{'name': 'application_owner'}])
        self.neon.run('create')
        self.assertEqual(fake.mutations, [('POST', '/branches')])
        self.assertEqual(fake.branches[-1], {'id': 'br-child', 'name': 'sbx-fixture-resource', 'parent_id': 'br-parent'})
        values = self.values()
        self.assertEqual((values['database'], values['user'], values['_branch_id']), ('application', 'application_owner', 'br-child'))
        url = urlsplit(values['url'])
        self.assertEqual((unquote(url.password), url.hostname), ('fixture:@?# /', 'fixture.neon.test'))

    def test_retry_creates_missing_endpoint_and_encodes_connection(self):
        fake = self.fake(branches=[self.parent, self.child], databases=[{'name': 'db name', 'owner_name': 'x'}])
        with patch.dict(os.environ, {'INPUT_PARENT': 'production', 'INPUT_ROLE': 'role:name', 'INPUT_DATABASE': 'db name'}):
            self.neon.run('create')
        self.assertEqual(fake.mutations, [('POST', '/endpoints')])
        url = urlsplit(self.values()['url'])
        self.assertEqual((unquote(url.username), unquote(url.password), unquote(url.path)), ('role:name', 'fixture:@?# /', '/db name'))

    def test_ready_retry_keeps_recorded_connection_after_shadow_database_creation(self):
        fake = self.fake(branches=[self.parent, self.child], endpoints={'br-child': [READY]},
                         databases=[{'name': 'application', 'owner_name': 'application_owner'},
                                    {'name': 'prisma_shadow', 'owner_name': 'application_owner'}],
                         roles=[{'name': 'application_owner', 'authentication_method': 'password'}])
        previous = {'database': 'application', 'user': 'application_owner', '_branch_id': 'br-child'}
        with patch.dict(os.environ, {'SBX_RESOURCE_STATUS': 'ready', 'SBX_PREVIOUS_OUTPUT': json.dumps(previous)}):
            self.neon.run('create')
        self.assertEqual({k: self.values()[k] for k in previous}, previous)
        self.assertEqual(fake.mutations, [])

    def test_missing_password_publishes_nothing(self):
        self.fake(branches=[self.parent, self.child], endpoints={'br-child': [READY]}).password = None
        with patch.dict(os.environ, {'INPUT_ROLE': 'role', 'INPUT_DATABASE': 'db'}), self.assertRaisesRegex(ValueError, 'no role password'):
            self.neon.run('create')
        self.assertFalse(self.output.exists())

    def test_same_name_replacement_is_never_reused_or_deleted(self):
        for action in ('create', 'delete'):
            fake = self.fake(branches=[{'id': 'br-replacement', 'name': 'sbx-fixture-resource'}])
            with self.subTest(action=action), patch.dict(os.environ, {
                    'SBX_PREVIOUS_OUTPUT': json.dumps({'_branch_id': 'br-recorded'}), 'SBX_RESOURCE_STATUS': 'ready'}):
                with self.assertRaisesRegex(ValueError, 'recorded Neon branch was replaced'):
                    self.neon.run(action)
                self.assertEqual(fake.mutations, [])

    def test_missing_recorded_branch_is_not_recreated_and_delete_is_idempotent(self):
        fake = self.fake(branches=[self.parent])
        with patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': json.dumps({'_branch_id': 'br-recorded'})}):
            with self.assertRaisesRegex(ValueError, 'recorded Neon branch is missing'):
                self.neon.run('create')
            self.neon.run('delete')
            self.neon.run('delete')
        self.assertEqual(fake.mutations, [])

    def test_delete_removes_only_the_owned_branch(self):
        fake = self.fake(branches=[self.parent, self.child])
        self.neon.run('delete')
        self.neon.run('delete')
        self.assertEqual(fake.mutations, [('DELETE', '/branches/br-child')])

    def test_default_protected_or_parent_branches_are_never_modified(self):
        for action in ('create', 'delete'):
            for flag in ('default', 'primary', 'protected'):
                fake = self.fake(branches=[{**self.child, flag: True}])
                with self.subTest(action=action, flag=flag), self.assertRaisesRegex(ValueError, 'default or protected'):
                    self.neon.run(action)
                self.assertEqual(fake.mutations, [])
        fake = self.fake(branches=[self.child])
        with patch.dict(os.environ, {'INPUT_PARENT': 'sbx-fixture-resource'}), self.assertRaisesRegex(ValueError, 'parent branch'):
            self.neon.run('create')
        self.assertEqual(fake.mutations, [])

    def test_list_prints_only_owned_branch_names(self):
        self.fake(branches=[self.parent, self.child])
        output = io.StringIO()
        with redirect_stdout(output):
            self.neon.run('list')
        self.assertEqual(output.getvalue(), 'sbx-fixture-resource\n')


class NeonProjectCheckTests(unittest.TestCase):
    def test_pointer_check_quotes_project_and_rejects_wrong_response(self):
        check = yaml.safe_load((CATALOG/'neon-project/package.yaml').read_text())['check']
        script = '\n'.join(check.splitlines()[1:-1])
        seen = []
        def urlopen(request, **kwargs):
            seen.append(request.full_url)
            return io.BytesIO(json.dumps({'project': {'id': os.environ['INPUT_PROJECT']}}).encode())
        with patch.dict(os.environ, {'INPUT_PROJECT': 'project/id?fragment', 'INPUT_API_KEY': 'fixture-private-key'}), patch('urllib.request.urlopen', side_effect=urlopen):
            exec(compile(script, '<Neon connection check>', 'exec'), {})
        self.assertTrue(seen[0].endswith('project%2Fid%3Ffragment'))
        with patch.dict(os.environ, {'INPUT_PROJECT': 'wanted', 'INPUT_API_KEY': 'fixture-private-key'}), patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"project":{"id":"other"}}')):
            with self.assertRaises(SystemExit) as caught:
                exec(compile(script, '<Neon connection check>', 'exec'), {})
        self.assertNotIn('fixture-private-key', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
