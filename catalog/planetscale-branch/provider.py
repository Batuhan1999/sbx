#!/usr/bin/env python3
"""PlanetScale Postgres development branches with recoverable one-time credentials."""
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import sbx_provider as sbx

CA_BUNDLE = '/etc/ssl/certs/ca-certificates.crt'


class PlanetScale(sbx.Api):
    def __init__(self):
        organization = quote(sbx.env('PSCALE_ORGANIZATION'), safe='')
        database = quote(sbx.env('PSCALE_DATABASE'), safe='')
        super().__init__('PlanetScale', f'https://api.planetscale.com/v1/organizations/{organization}/databases/{database}',
                         {'Authorization': sbx.env('PSCALE_SERVICE_TOKEN_ID') + ':' + sbx.env('PSCALE_SERVICE_TOKEN')},
                         hint='check branch state and service token access')

    def items(self, path):
        def page(number):
            result = self.request(path + '?' + urlencode({'page': number, 'per_page': 100}))
            following = result.get('next_page')
            if following is not None and (not isinstance(following, int) or following <= number):
                raise sbx.ProviderError('PlanetScale returned invalid pagination; no resource was changed')
            return result['data'], following
        return sbx.all_pages('PlanetScale', page, 1)

    @staticmethod
    def matching(rows, name, kind):
        return sbx.only(rows, f'PlanetScale {kind} name', lambda row: row['name'] == name and not row.get('deleted_at'))

    @staticmethod
    def owned_branch(branch, name, parent, database):
        if branch['name'] != name or branch.get('production') is not False or branch.get('deletion_protected'):
            raise sbx.ProviderError('refusing to modify a production, protected, or unexpected PlanetScale branch')
        if name in (parent, database.get('default_branch')):
            raise sbx.ProviderError('refusing to modify the PlanetScale parent or default branch')
        if branch.get('parent_branch') and branch['parent_branch'] != parent:
            raise sbx.ProviderError('the PlanetScale branch belongs to another parent; it is unchanged')

    def ready(self, path, value, timeout):
        if value.get('ready'):
            return value
        def poll():
            current = self.request(path)
            return current if current.get('ready') else None
        return sbx.wait('PlanetScale resource', poll, timeout, interval=3)

    @staticmethod
    def outputs(branch, role, password, sslrootcert=CA_BUNDLE):
        raw = role['access_host_url']
        endpoint = urlsplit(raw if '://' in raw else 'postgresql://' + raw) if isinstance(raw, str) else None
        if not endpoint or endpoint.scheme not in ('postgres', 'postgresql') or not endpoint.hostname:
            raise sbx.ProviderError('PlanetScale returned an invalid Postgres endpoint')
        host, port = endpoint.hostname, endpoint.port or 5432
        user, database = role['username'], role['database_name']
        if any(not isinstance(value, str) or not value for value in (password, user, database)):
            raise sbx.ProviderError('PlanetScale returned incomplete connection values')
        if sslrootcert != 'system' and not PurePosixPath(sslrootcert).is_absolute():
            raise sbx.ProviderError('sslrootcert must name the sandbox CA bundle or use system for native libpq')
        authority = f'{quote(user, safe="")}:{quote(password, safe="")}@{f"[{host}]" if ":" in host else host}:{port}'
        query = [(k, v) for k, v in parse_qsl(endpoint.query, keep_blank_values=True) if k not in ('sslmode', 'sslrootcert')]
        url = urlunsplit(('postgresql', authority, '/' + quote(database, safe=''),
                          urlencode([*query, ('sslmode', 'verify-full'), ('sslrootcert', sslrootcert)]), ''))
        return {'url': url, 'password': password, 'host': host, 'port': str(port), 'user': user,
                'database': database, 'branch': branch['name'], '_branch_id': branch['id'], '_role_id': role['id']}

    def run(self, action):
        database = self.request('')
        if database.get('kind') != 'postgresql':
            raise sbx.ProviderError('this provisioner supports PlanetScale PostgreSQL; Vitess/MySQL and Neki need another adapter')
        branches = self.items('/branches')
        if action == 'list':
            print('\n'.join(b['name'] for b in branches if sbx.owned(b['name']) and not b.get('deleted_at')))
            return
        name, parent = sbx.resource_name(), sbx.option('parent', 'main')
        if name in (parent, database.get('default_branch')):
            raise sbx.ProviderError('refusing to modify the PlanetScale parent or default branch')
        branch = self.matching(branches, name, 'branch')
        path = '/branches/' + quote(name, safe='')
        previous = sbx.previous('PlanetScale connection', '_branch_id', '_role_id')
        if branch:
            self.owned_branch(branch, name, parent, database)
        sbx.recorded('PlanetScale branch', branch and branch['id'], previous.get('_branch_id'), creating=action == 'create')
        if action == 'delete':
            if branch:
                self.request(path, 'DELETE', missing_ok=True)
            return
        if not branch:
            source = self.matching(branches, parent, 'parent branch')
            if not source:
                raise sbx.ProviderError('the selected PlanetScale parent branch does not exist')
            region = sbx.option('region') or (source.get('region') or {}).get('slug')
            branch = self.request('/branches', 'POST', {'name': name, 'parent_branch': parent, 'cluster_size': 'PS_DEV',
                                                        'create_database_if_missing': False, **({'region': region} if region else {})})
        branch = self.ready(path, branch, 420)
        self.owned_branch(branch, name, parent, database)
        role = self.matching(self.items(path + '/roles'), name, 'role')
        if role:
            if role.get('default') or (role.get('branch') or {}).get('id', branch['id']) != branch['id']:
                raise sbx.ProviderError('refusing to change an unexpected or default PlanetScale role')
            if previous.get('_role_id') == role['id'] and previous.get('_branch_id') == branch['id'] and previous.get('password'):
                password = previous['password']
            elif sbx.status() == 'creating':
                role = self.request(path + '/roles/' + quote(role['id'], safe='') + '/reset', 'POST')
                password = role.get('password')
            else:
                raise sbx.ProviderError('the active PlanetScale role has no matching saved credential; recover explicitly without resetting it')
        else:
            role = self.request(path + '/roles', 'POST', {'name': name, 'inherited_roles': ['postgres']})
            password = role.get('password')
        if not isinstance(password, str) or not password:
            raise sbx.ProviderError('PlanetScale returned no one-time password; retry provisioning before starting the app')
        role = self.ready(path + '/roles/' + quote(role['id'], safe=''), role, 90)
        sbx.write_outputs(self.outputs(branch, role, password, sbx.option('sslrootcert', CA_BUNDLE)))


if __name__ == '__main__':
    sbx.main(lambda action: PlanetScale().run(action))
