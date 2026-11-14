#!/usr/bin/env python3
"""Neon branch lifecycle using the project's paginated API."""
from urllib.parse import quote, urlencode

import sbx_provider as sbx

SYSTEM_DATABASES = ('postgres', 'template0', 'template1')


class Neon(sbx.Api):
    def __init__(self):
        super().__init__('Neon', 'https://console.neon.tech/api/v2/projects/' + quote(sbx.env('NEON_PROJECT'), safe=''),
                         {'Authorization': 'Bearer ' + sbx.env('NEON_API_KEY')},
                         hint='check project access and branch state')

    def branches(self):
        def page(cursor):
            params = {'limit': 100, 'sort_by': 'created_at', 'sort_order': 'asc', **({'cursor': cursor} if cursor else {})}
            response = self.request('/branches?' + urlencode(params))
            return response['branches'], (response.get('pagination') or {}).get('next')
        return sbx.all_pages('Neon', page)

    def matching(self, name):
        return sbx.only(self.branches(), 'Neon branch name', lambda branch: name in (branch['name'], branch['id']))

    def parent(self):
        selected = sbx.option('parent')
        if selected:
            parent = self.matching(selected)
            if not parent:
                raise sbx.ProviderError('the requested Neon parent branch does not exist')
            return parent
        branches = self.branches()
        defaults = [branch for branch in branches if branch.get('default')] or [b for b in branches if b.get('primary')]
        if len(defaults) != 1:
            raise sbx.ProviderError('Neon has no single default branch; select a parent branch explicitly')
        return defaults[0]

    def connection_selection(self, branch, previous=None):
        previous = previous or {}
        role, database = sbx.option('role'), sbx.option('database')
        if role and database:
            return role, database
        saved_role = not role and previous.get('user')
        role = role or saved_role
        database = database or previous.get('database')
        path = '/branches/' + quote(branch['id'], safe='')
        databases = self.request(path + '/databases')['databases']
        candidates = [item for item in databases if (item['name'] == database if database else item['name'] not in SYSTEM_DATABASES)]
        if len(candidates) != 1:
            raise sbx.ProviderError('select a Neon database explicitly; no single application database was found')
        database = candidates[0]['name']
        if not role or saved_role:
            role = role or candidates[0].get('owner_name')
            owner = next((item for item in self.request(path + '/roles')['roles'] if item['name'] == role), None)
            if not owner or owner.get('authentication_method', 'password') != 'password':
                raise sbx.ProviderError('the selected Neon role has no password login; select a migration-capable role explicitly')
        return role, database

    @staticmethod
    def guard(branch):
        selected = sbx.option('parent')
        if branch.get('default') or branch.get('primary') or branch.get('protected'):
            raise sbx.ProviderError('refusing to modify a default or protected Neon branch')
        if selected and selected in (branch['name'], branch['id']):
            raise sbx.ProviderError('refusing to modify the parent branch')

    def run(self, action):
        if action == 'list':
            print('\n'.join(b['name'] for b in self.branches() if sbx.owned(b['name'])))
            return
        name = sbx.resource_name()
        previous = sbx.previous('Neon connection', '_branch_id')
        branch = self.matching(name)
        sbx.recorded('Neon branch', branch and branch['id'], previous.get('_branch_id'), creating=action == 'create')
        if branch:
            self.guard(branch)
        if action == 'delete':
            if branch:
                self.request('/branches/' + quote(branch['id'], safe=''), 'DELETE', missing_ok=True)
            return
        source = branch or self.parent()
        role, database = self.connection_selection(source, previous)
        if not branch:
            branch = self.request('/branches', 'POST', {
                'branch': {'name': name, 'parent_id': source['id']}, 'endpoints': [{'type': 'read_write'}],
            })['branch']
        elif not any(e['type'] == 'read_write' for e in self.endpoints(branch)):
            self.request('/endpoints', 'POST', {'endpoint': {'branch_id': branch['id'], 'type': 'read_write'}})
        ready = sbx.wait('Neon compute', lambda: next((e for e in self.endpoints(branch) if e['type'] == 'read_write' and e.get('host')
                                                      and e.get('current_state') in ('active', 'idle')), None), 90)
        path = f'/branches/{quote(branch["id"], safe="")}/roles/{quote(role, safe="")}/reveal_password'
        password = (self.request(path) or {}).get('password')
        if not isinstance(password, str) or not password:
            raise sbx.ProviderError('Neon returned no role password; no connection values were published')
        host = ready['host']
        sbx.write_outputs({
            'url': f'postgresql://{quote(role, safe="")}:{quote(password, safe="")}@{host}/{quote(database, safe="")}?sslmode=require',
            'host': host, 'port': '5432', 'user': role, 'password': password, 'database': database, '_branch_id': branch['id'],
        })

    def endpoints(self, branch):
        return self.request('/branches/' + quote(branch['id'], safe='') + '/endpoints')['endpoints']


if __name__ == '__main__':
    sbx.main(lambda action: Neon().run(action))
