"""Upstash Redis branch contracts against a fake Upstash API, without a hosted account."""
import json
import os
import sys
import unittest
from unittest.mock import patch

from catalog_scripts import CATALOG, FakeApi, ScriptCase, Status, load


def database(**changes):
    return {'database_id': 'db-parent', 'database_name': 'development', 'database_type': 'Pay as You Go',
            'region': 'global', 'state': 'active', 'endpoint': 'parent-fixture', 'port': 6379,
            'password': 'parent-password', 'rest_token': 'parent-token', 'tls': True, **changes}


def branch(**changes):
    return database(**{'database_id': 'db-branch', 'database_name': 'sbx-audit-resource',
                       'root_database_id': 'db-parent', 'source_database_id': 'db-parent', 'endpoint': 'branch-fixture',
                       'password': 'branch:@?# /', 'rest_token': 'branch-token', **changes})


class FakeUpstash(FakeApi):
    """The account's databases and branches. pending is how many reads of a
    branch still find it creating; failing removes a branch while it is
    created; detail changes what a database's own page says."""
    base = 'https://api.upstash.com/v2'

    def __init__(self, *databases):
        super().__init__()
        self.databases = {d['database_id']: d for d in databases}
        self.pending, self.failing, self.created, self.detail = 0, False, [], {}

    def handle(self, method, path, body):
        if (method, path) == ('GET', '/redis/databases'):
            return list(self.databases.values())
        identifier = path.removeprefix('/redis/database/')
        if method == 'POST' and identifier.endswith('/branch'):
            source = self.databases.get(identifier.removesuffix('/branch'))
            if source is None:
                raise Status(404)
            if source.get('database_type') == 'free':
                raise Status(400)
            self.created.append((source['database_id'], body))
            made = branch(database_id='db-new', database_name=body['name'], state='creating',
                          root_database_id=source.get('root_database_id') or source['database_id'],
                          source_database_id=source['database_id'])
            self.databases['db-new'] = made
            return made
        if identifier not in self.databases:
            raise Status(404)
        if method == 'DELETE':
            del self.databases[identifier]
            return None
        if self.failing:
            del self.databases[identifier]
            raise Status(404)
        if self.pending:
            self.pending -= 1
            return {**self.databases[identifier], 'state': 'creating'}
        if self.databases[identifier]['state'] == 'creating':
            self.databases[identifier]['state'] = 'active'
        return {**self.databases[identifier], **self.detail}


class UpstashBranchProviderTests(ScriptCase):
    environment = {'UPSTASH_EMAIL': 'fixture@example.test', 'UPSTASH_API_KEY': 'fixture-api-key',
                   'INPUT_PARENT': 'development'}

    def setUp(self):
        super().setUp()
        self.enterContext(patch('time.sleep'))
        self.enterContext(patch.object(sys, 'path', [str(CATALOG / 'upstash-redis-branch'), *sys.path]))
        self.module = load('upstash-redis-branch')
        self.api = self.module.UpstashBranch()

    def upstash(self, *databases):
        return FakeUpstash(*databases).serve(self)

    def previous(self, value):
        return patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': json.dumps(value)})

    def test_create_branches_the_selected_parent_and_publishes_the_branch_credentials(self):
        upstash = self.upstash(database(), database(database_id='db-other', database_name='other'))
        self.api.run('create')
        self.assertEqual(upstash.created, [('db-parent', {'name': 'sbx-audit-resource'})])
        values = self.values()
        self.assertEqual(values['database_id'], 'db-new')
        self.assertEqual(values['host'], 'branch-fixture.upstash.io')
        self.assertEqual(values['rest_token'], 'branch-token')
        self.assertTrue(values['url'].startswith('rediss://default:branch%3A%40%3F%23%20%2F@branch-fixture.upstash.io:6379'))

    def test_a_branch_can_be_the_parent_selected_by_id(self):
        upstash = self.upstash(database(), branch(database_id='db-staging', database_name='staging'))
        with patch.dict(os.environ, {'INPUT_PARENT': 'db-staging'}):
            self.api.run('create')
        self.assertEqual(upstash.created, [('db-staging', {'name': 'sbx-audit-resource'})])
        self.assertEqual(upstash.databases['db-new']['root_database_id'], 'db-parent')

    def test_a_creating_branch_is_polled_until_active(self):
        upstash = self.upstash(database())
        upstash.pending = 3
        self.api.run('create')
        self.assertEqual(self.values()['database_id'], 'db-new')

    def test_retry_reuses_the_branch_without_creating_another(self):
        upstash = self.upstash(database(), branch())
        with self.previous({'database_id': 'db-branch'}), patch.dict(os.environ, {'SBX_RESOURCE_STATUS': 'ready'}):
            self.api.run('create')
        self.assertEqual((upstash.mutations, self.values()['database_id']), ([], 'db-branch'))

    def test_a_branch_removed_during_creation_is_reported_and_not_recreated(self):
        upstash = self.upstash(database())
        upstash.failing = True
        with self.assertRaisesRegex(ValueError, 'could not create the branch'):
            self.api.run('create')
        self.assertEqual(self.values(), {'database_id': 'db-new'})   # the identity only, no credentials
        with self.previous(self.values()), self.assertRaisesRegex(ValueError, 'branch is missing'):
            self.api.run('create')
        self.assertEqual(len(upstash.created), 1)

    def test_a_timed_out_branch_is_resumed_by_its_recorded_id(self):
        upstash = self.upstash(database())
        upstash.pending = 100
        with patch('time.monotonic', side_effect=[0, 151]), self.assertRaisesRegex(ValueError, 'not ready'):
            self.api.run('create')
        self.assertEqual(self.values(), {'database_id': 'db-new'})
        upstash.pending = 0
        with self.previous(self.values()):
            self.api.run('create')
        self.assertEqual((len(upstash.created), self.values()['host']), (1, 'branch-fixture.upstash.io'))

    def test_ineligible_or_unclear_parents_never_create_a_branch(self):
        for parent, error in ((database(database_type='free'), 'paid plan'),
                              (database(region='eu-west-1'), 'global'),
                              (database(state='suspended'), 'suspended')):
            upstash = self.upstash(parent)
            with self.subTest(parent=parent), self.assertRaisesRegex(ValueError, error):
                self.api.run('create')
            self.assertEqual(upstash.mutations, [])
        upstash = self.upstash(database(), database(database_id='db-twin'))
        with self.assertRaisesRegex(ValueError, 'select it by ID'):
            self.api.run('create')
        for value, error in (('missing', 'does not exist'), ('', 'select the Upstash database')):
            with self.subTest(parent=value), patch.dict(os.environ, {'INPUT_PARENT': value}), \
                    self.assertRaisesRegex(ValueError, error):
                self.api.run('create')
        self.assertEqual(upstash.mutations, [])

    def test_a_branch_whose_own_page_disagrees_publishes_nothing(self):
        for detail in ({'root_database_id': 'db-elsewhere'}, {'database_id': 'db-replacement'}, {'state': 'suspended'}):
            upstash = self.upstash(database(), branch())
            upstash.detail = detail
            with self.subTest(detail=detail), self.assertRaisesRegex(ValueError, 'identity changed|suspended'):
                self.api.run('create')
            self.assertEqual(upstash.mutations, [])
            self.assertFalse(self.output.exists())

    def test_saved_identity_refuses_a_same_name_replacement(self):
        for action in ('create', 'delete'):
            upstash = self.upstash(database(), branch())
            with self.subTest(action=action), self.previous({'database_id': 'recorded-id'}), \
                    self.assertRaisesRegex(ValueError, 'branch was replaced'):
                self.api.run(action)
            self.assertEqual(upstash.mutations, [])

    def test_delete_removes_only_the_branch_and_never_a_main_database(self):
        upstash = self.upstash(database(), database(database_id='db-main-same-name', database_name='sbx-audit-resource'),
                               branch())
        self.api.run('delete')
        self.api.run('delete')
        self.assertEqual(sorted(upstash.databases), ['db-main-same-name', 'db-parent'])

    def test_list_names_only_sbx_branches(self):
        self.upstash(database(), database(database_id='db-x', database_name='sbx-main-database'), branch(),
                     branch(database_id='db-staging', database_name='staging'))
        with patch('builtins.print') as printed:
            self.api.run('list')
        printed.assert_called_once_with('sbx-audit-resource')


if __name__ == '__main__':
    unittest.main()
