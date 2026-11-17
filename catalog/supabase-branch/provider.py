#!/usr/bin/env python3
"""Supabase preview branch lifecycle through the Management API."""
import re
from urllib.parse import quote

import sbx_provider as sbx

SETTING_UP = ('CREATING_PROJECT', 'RUNNING_MIGRATIONS')
FAILED = ('MIGRATIONS_FAILED', 'FUNCTIONS_FAILED')
SHARED_POOLER = '.pooler.supabase.com'  # Supavisor: IPv4, unlike the database's own host
SESSION_PORT = '5432'  # the shared pooler's session mode, fit for migrations


def records(value, what):
    """A reply that must be a list of objects."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise sbx.ProviderError(f'Supabase returned an unexpected {what}; no connection values were published')
    return value


class Supabase(sbx.Api):
    def __init__(self):
        super().__init__('Supabase', 'https://api.supabase.com/v1',
                         {'Authorization': 'Bearer ' + sbx.env('SUPABASE_ACCESS_TOKEN')},
                         hint="check the token, the project's plan and branch state")
        self.project = sbx.env('SUPABASE_PROJECT')

    def branches(self):
        return records(self.request('/projects/' + quote(self.project, safe='') + '/branches'), 'branch list')

    def matching(self, name):
        return sbx.only(self.branches(), 'Supabase branch name', lambda branch: branch['name'] == name)

    def guard(self, branch):
        if branch.get('is_default') or branch.get('persistent') or branch['project_ref'] == self.project:
            raise sbx.ProviderError('refusing to modify a default or persistent Supabase branch')
        if branch.get('parent_project_ref') != self.project:
            raise sbx.ProviderError('the Supabase branch belongs to another project; no resource was changed')

    def branch(self, name):
        return self.request('/projects/' + quote(self.project, safe='') + '/branches/' + quote(name, safe=''), missing_ok=True)

    def ready(self, name, ref):
        branch = self.branch(name)
        if not branch:  # a new branch may not be listed yet
            return None
        if branch['project_ref'] != ref:
            raise sbx.ProviderError('the recorded Supabase branch was replaced; no connection values were published')
        if branch.get('status') in FAILED:
            raise sbx.ProviderError(f'Supabase could not set up the branch ({branch["status"]}); see its logs in the dashboard')
        if branch.get('status') in SETTING_UP:
            return None
        detail = self.request('/branches/' + quote(branch['project_ref'], safe=''))
        if not isinstance(detail, dict):
            raise sbx.ProviderError('Supabase returned unexpected branch details; no connection values were published')
        return detail if detail.get('status') == 'ACTIVE_HEALTHY' else None

    def keys(self, ref):
        keys = records(self.request('/projects/' + quote(ref, safe='') + '/api-keys?reveal=true'), 'API key list')

        def pick(legacy, kind):
            usable = [k for k in keys if isinstance(k.get('api_key'), str) and k['api_key']]
            found = next((k for k in usable if k.get('name') == legacy and k.get('type') in ('legacy', None)), None) \
                or next((k for k in usable if k.get('type') == kind), None)
            if not found:
                raise sbx.ProviderError(f'Supabase returned no {legacy} key for the branch; no connection values were published')
            return found['api_key']
        return pick('anon', 'publishable'), pick('service_role', 'secret')

    def pooler(self, ref):
        entries = records(self.request('/projects/' + quote(ref, safe='') + '/config/database/pooler'), 'pooler list')
        primary = sbx.only(entries, 'Supabase shared pooler', lambda entry: entry.get('database_type') == 'PRIMARY'
                           and str(entry.get('db_host', '')).endswith(SHARED_POOLER))
        if not primary:
            raise sbx.ProviderError('Supabase returned no shared pooler for the branch; no connection values were published')
        return primary

    def run(self, action):
        if action == 'list':
            print('\n'.join(b['name'] for b in self.branches() if sbx.owned(b['name'])))
            return
        name = sbx.resource_name()
        previous = sbx.previous('Supabase connection', '_branch_ref')
        branch = self.matching(name)
        sbx.recorded('Supabase branch', branch and branch['project_ref'], previous.get('_branch_ref'),
                     creating=action == 'create')
        if branch:
            self.guard(branch)
        if action == 'delete':
            if branch:
                try:
                    self.request('/branches/' + quote(branch['project_ref'], safe=''), 'DELETE', missing_ok=True)
                except sbx.HttpError:
                    if branch.get('status') in SETTING_UP:
                        raise sbx.ProviderError('Supabase is still setting up the branch and refused to delete it; '
                                                'retry once it is ready') from None
                    raise
            return
        if branch and branch.get('deletion_scheduled_at'):
            raise sbx.ProviderError('the Supabase branch is scheduled for deletion; restore or delete it, then retry')
        if not branch:
            with_data = sbx.option('with_data', 'false')
            if with_data not in ('true', 'false'):
                raise sbx.ProviderError('with_data is true or false')
            try:
                branch = self.request('/projects/' + quote(self.project, safe='') + '/branches', 'POST', {
                    'branch_name': name, 'persistent': False, 'with_data': with_data == 'true'})
            except sbx.HttpError as error:
                if error.status == 402:
                    raise sbx.ProviderError("Supabase refused the branch (HTTP 402): branching needs a paid plan "
                                            "on the project's organization, and copying data a plan that allows it") from None
                raise
            sbx.write_outputs({'_branch_ref': branch['project_ref']})
        ref = branch['project_ref']
        if not re.fullmatch(r'[a-z]{20}', ref):
            raise sbx.ProviderError('Supabase returned an unexpected branch ref; no connection values were published')
        detail = sbx.wait('Supabase branch', lambda: self.ready(name, ref), 600, interval=5)
        password = detail.get('db_pass')
        if not isinstance(password, str) or not password:
            raise sbx.ProviderError('Supabase returned no database password; no connection values were published')
        pooler = self.pooler(ref)
        anon, service_role = self.keys(ref)
        host, user, database = pooler['db_host'], pooler['db_user'], pooler['db_name']
        sbx.write_outputs({
            'url': f'postgresql://{quote(user, safe="")}:{quote(password, safe="")}@{host}:{SESSION_PORT}/'
                   f'{quote(database, safe="")}?sslmode=require',
            'host': host, 'port': SESSION_PORT, 'user': user, 'password': password, 'database': database,
            'api_url': f'https://{ref}.supabase.co', 'anon_key': anon, 'service_role_key': service_role,
            'ref': ref, '_branch_ref': ref,
        })


if __name__ == '__main__':
    sbx.main(lambda action: Supabase().run(action))
