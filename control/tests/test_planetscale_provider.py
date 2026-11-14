"""PlanetScale provider contracts against a fake PlanetScale API; no account or cloud mutations required."""
from contextlib import redirect_stdout
import io
import json
import os
import re
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from catalog_scripts import FakeApi, ScriptCase, Status, load

NAME = 'sbx-audit-resource'


class FakePlanetScale(FakeApi):
    """One database's branches and their roles. A new branch or role is not
    ready when created and is on its next read; page_size splits lists into
    pages; next_page, when set, is what every page says comes next."""
    base = 'https://api.planetscale.com/v1/organizations/fixture-org/databases/fixture-db'

    def __init__(self, database, branches=(), roles=()):
        super().__init__()
        self.database = database
        self.branches = {b['name']: dict(b) for b in branches}
        self.roles = {b['name']: [dict(r) for r in roles] for b in branches if b['name'] == NAME}
        self.page_size, self.next_page, self.created = 100, None, []

    def page(self, rows, query):
        number = int(query['page'][0])
        start = (number - 1) * self.page_size
        following = number + 1 if start + self.page_size < len(rows) else None
        return {'data': rows[start:start + self.page_size],
                'next_page': self.next_page if self.next_page is not None else following}

    def handle(self, method, path, body):
        parsed = urlsplit(path)
        path, query = parsed.path, parse_qs(parsed.query)
        if (method, path) == ('GET', ''):
            return self.database
        if (method, path) == ('GET', '/branches'):
            return self.page(list(self.branches.values()), query)
        if (method, path) == ('POST', '/branches'):
            self.created.append(body)
            self.branches[body['name']] = branch = {'id': 'branch-id', 'name': body['name'], 'production': False,
                                                    'parent_branch': body['parent_branch'], 'ready': False}
            self.roles[body['name']] = []
            return dict(branch)
        found = re.fullmatch(r'/branches/([^/]+)(/roles(?:/([^/]+)(/reset)?)?)?', path)
        if not found or found[1] not in self.branches:
            raise Status(404)
        name, roles, role_id, reset = found.groups()
        if not roles:
            if method == 'DELETE':
                del self.branches[name]
                self.roles.pop(name, None)
                return None
            return self.read(self.branches[name])
        if not role_id:
            if method == 'POST':
                self.created.append(body)
                role = {'id': 'role-id', 'name': body['name'], 'ready': False, 'default': False,
                        'username': 'app.user:branch', 'database_name': 'app db',
                        'access_host_url': 'postgresql://[2001:db8::5]:6543?connect_timeout=10'}
                self.roles[name].append(role)
                return {**role, 'password': 'fixture:@? /'}
            return self.page(self.roles[name], query)
        role = next((r for r in self.roles[name] if r['id'] == role_id), None)
        if role is None:
            raise Status(404)
        if reset:
            return {**role, 'password': 'recovered-password'}
        return self.read(role)

    @staticmethod
    def read(row):
        found = dict(row)
        row['ready'] = True
        return found


class PlanetScaleProviderTests(ScriptCase):
    environment = {'PSCALE_ORGANIZATION': 'fixture-org', 'PSCALE_DATABASE': 'fixture-db',
                   'PSCALE_SERVICE_TOKEN_ID': 'fixture-token-id', 'PSCALE_SERVICE_TOKEN': 'fixture-secret',
                   'INPUT_PARENT': 'main', 'INPUT_REGION': ''}

    def setUp(self):
        super().setUp()
        self.enterContext(patch('time.sleep'))
        self.module = load('planetscale-branch')
        self.client = self.module.PlanetScale()
        self.database = {'kind': 'postgresql', 'default_branch': 'main'}
        self.parent = {'id': 'parent-id', 'name': 'main', 'production': True, 'ready': True,
                       'region': {'slug': 'fixture-region'}}
        self.branch = {'id': 'branch-id', 'name': NAME, 'production': False, 'parent_branch': 'main', 'ready': True}
        self.role = {'id': 'role-id', 'name': NAME, 'ready': True, 'default': False,
                     'username': 'app.user:branch', 'database_name': 'app db',
                     'access_host_url': 'postgresql://[2001:db8::5]:6543?connect_timeout=10'}

    def planetscale(self, *branches, role=None):
        return FakePlanetScale(self.database, branches or (self.parent, self.branch), [role or self.role]).serve(self)

    def test_a_new_branch_and_admin_role_are_created_with_the_documented_fields(self):
        planetscale = self.planetscale(self.parent)
        self.client.run('create')
        self.assertEqual(planetscale.created, [
            {'name': NAME, 'parent_branch': 'main', 'cluster_size': 'PS_DEV', 'create_database_if_missing': False,
             'region': 'fixture-region'},
            {'name': NAME, 'inherited_roles': ['postgres']}])
        values = self.values()
        url = urlsplit(values['url'])
        self.assertEqual((url.hostname, url.port), ('2001:db8::5', 6543))
        self.assertEqual(unquote(url.username), 'app.user:branch')
        self.assertEqual(unquote(url.password), 'fixture:@? /')
        self.assertEqual(unquote(url.path), '/app db')
        self.assertEqual(parse_qs(url.query), {'connect_timeout': ['10'], 'sslmode': ['verify-full'],
                                              'sslrootcert': ['/etc/ssl/certs/ca-certificates.crt']})
        self.assertEqual((values['_branch_id'], values['_role_id']), ('branch-id', 'role-id'))

    def test_requests_carry_the_service_token_in_a_header(self):
        planetscale = self.planetscale()
        with redirect_stdout(io.StringIO()):
            self.client.run('list')
        for request in planetscale.requests:
            self.assertEqual(request['headers']['Authorization'], 'fixture-token-id:fixture-secret')
            self.assertNotIn('fixture-secret', request['url'])

    def test_ready_retry_reuses_saved_password_without_reset(self):
        planetscale = self.planetscale()
        previous = self.module.PlanetScale.outputs(self.branch, self.role, 'saved-password')
        with patch.dict(os.environ, {'SBX_RESOURCE_STATUS': 'ready', 'SBX_PREVIOUS_OUTPUT': json.dumps(previous)}):
            self.client.run('create')
        self.assertEqual(self.values(), previous)
        self.assertEqual(planetscale.mutations, [])

    def test_ca_bundle_override_keeps_certificate_verification_enabled(self):
        for root in ('/custom/trust/roots.pem', 'system'):
            with self.subTest(root=root):
                values = self.module.PlanetScale.outputs(self.branch, self.role, 'fixture-password', root)
                query = parse_qs(urlsplit(values['url']).query)
                self.assertEqual(query['sslrootcert'], [root])
                self.assertEqual(query['sslmode'], ['verify-full'])
        with self.assertRaisesRegex(ValueError, 'sandbox CA bundle'):
            self.module.PlanetScale.outputs(self.branch, self.role, 'fixture-password', '')

    def test_interrupted_creation_recovers_only_its_owned_role(self):
        planetscale = self.planetscale()
        self.client.run('create')
        self.assertEqual(self.values()['password'], 'recovered-password')
        self.assertEqual(planetscale.mutations, [('POST', f'/branches/{NAME}/roles/role-id/reset')])

    def test_active_or_default_roles_are_never_reset(self):
        for role, status, error in ((self.role, 'ready', 'active PlanetScale role'),
                                    ({**self.role, 'default': True}, 'creating', 'default PlanetScale role')):
            with self.subTest(error=error), patch.dict(os.environ, {'SBX_RESOURCE_STATUS': status}):
                planetscale = self.planetscale(role=role)
                with self.assertRaisesRegex(ValueError, error):
                    self.client.run('create')
                self.assertFalse(self.output.exists())
                self.assertEqual(planetscale.mutations, [])

    def test_replaced_branch_is_not_adopted_or_deleted(self):
        planetscale = self.planetscale()
        with patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': json.dumps({'_branch_id': 'original-id'})}):
            for action in ('create', 'delete'):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'branch was replaced'):
                    self.client.run(action)
        self.assertEqual(planetscale.mutations, [])

    def test_parent_production_default_and_protected_branches_are_preserved(self):
        for settings, parent, default in (({'production': True}, 'main', 'main'),
                ({'deletion_protected': True}, 'main', 'main'), ({}, NAME, 'main'), ({}, 'main', NAME)):
            with self.subTest(settings=settings, parent=parent, default=default):
                self.database['default_branch'] = default
                planetscale = self.planetscale(self.parent, {**self.branch, **settings})
                with patch.dict(os.environ, {'INPUT_PARENT': parent}), self.assertRaisesRegex(ValueError, 'refusing'):
                    self.client.run('delete')
                self.assertEqual(planetscale.mutations, [])

    def test_paginated_lists_are_complete_and_bad_pagination_is_refused(self):
        planetscale = self.planetscale()
        planetscale.page_size = 1
        self.assertEqual([row['name'] for row in self.client.items('/branches')], ['main', NAME])
        planetscale.next_page = 1
        with self.assertRaisesRegex(ValueError, 'pagination'):
            self.client.items('/branches')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.client.matching([self.branch, self.branch], NAME, 'branch')

    def test_delete_targets_only_the_owned_branch_and_is_idempotent(self):
        planetscale = self.planetscale()
        self.client.run('delete')
        self.client.run('delete')
        self.assertEqual(list(planetscale.branches), ['main'])
        self.assertEqual(planetscale.mutations, [('DELETE', f'/branches/{NAME}')])

    def test_list_prints_only_resource_names_not_credentials(self):
        self.planetscale()
        output = io.StringIO()
        with redirect_stdout(output):
            self.client.run('list')
        self.assertEqual(output.getvalue().strip(), NAME)

    def test_other_database_engines_fail_before_any_mutation(self):
        self.database['kind'] = 'mysql'
        planetscale = self.planetscale(self.parent)
        with self.assertRaisesRegex(ValueError, 'Vitess/MySQL'):
            self.client.run('create')
        self.assertEqual(planetscale.mutations, [])


if __name__ == '__main__':
    unittest.main()
