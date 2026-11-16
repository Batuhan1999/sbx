#!/usr/bin/env python3
"""Upstash Redis branches: a copy of a parent database per sandbox."""
import re
from urllib.parse import quote

import sbx_provider as sbx
from upstash_redis import Upstash, connection


def root(database):
    return database.get('root_database_id') or database['database_id']


class UpstashBranch(Upstash):
    def __init__(self):
        super().__init__()
        self.hint = 'branching needs a global database on a paid plan and an account with a payment method'

    def parent(self):
        selected = sbx.option('parent')
        if not selected:
            raise sbx.ProviderError('select the Upstash database or branch to copy')
        parent = sbx.only(self.databases(), 'Upstash parent name (select it by ID)',
                          lambda d: selected in (d.get('database_name'), d.get('database_id')))
        if not parent:
            raise sbx.ProviderError('the selected Upstash parent database does not exist')
        if parent.get('database_type') == 'free' or parent.get('region') not in (None, 'global'):
            raise sbx.ProviderError('only global Upstash databases on a paid plan can be branched; select another parent')
        if parent.get('state') == 'suspended':
            raise sbx.ProviderError('the Upstash parent database is suspended; resolve account or budget limits first')
        return parent

    def ready(self, identifier, name, expected_root):
        database = self.request('/redis/database/' + quote(identifier, safe=''), missing_ok=True)
        if database is None:
            raise sbx.ProviderError('Upstash could not create the branch and removed it; check the parent and account eligibility')
        if (database.get('database_id'), database.get('database_name'), database.get('root_database_id')) != \
                (identifier, name, expected_root):
            raise sbx.ProviderError('Upstash branch identity changed; no connection values were published')
        if database.get('state') == 'suspended':
            raise sbx.ProviderError('the Upstash branch is suspended; resolve account or budget limits before retrying')
        return database if database.get('state') == 'active' else None

    def run(self, action):
        branches = [d for d in self.databases() if d.get('root_database_id')]
        if action == 'list':
            print('\n'.join(d['database_name'] for d in branches if sbx.owned(d.get('database_name'))))
            return
        name = sbx.resource_name()
        saved_id = sbx.previous('Upstash branch', 'database_id').get('database_id')
        branch = sbx.only(branches, 'Upstash branch name', lambda d: d.get('database_name') == name)
        sbx.recorded('Upstash branch', branch and branch.get('database_id'), saved_id, creating=action == 'create')
        if action == 'delete':
            if branch:
                self.request('/redis/database/' + quote(branch['database_id'], safe=''), 'DELETE', missing_ok=True)
            return
        budget = sbx.option('budget', '1')
        if not re.fullmatch(r'[1-9][0-9]*', budget):
            raise sbx.ProviderError('choose a positive whole number of US dollars as the monthly budget')
        if branch:
            expected_root = branch['root_database_id']
        else:
            parent = self.parent()
            expected_root = root(parent)
            branch = self.request('/redis/database/' + quote(parent['database_id'], safe='') + '/branch', 'POST',
                                  {'name': name})
            # Recorded before polling: a retry then finds this branch, or reports it gone, instead of making another.
            sbx.write_outputs({'database_id': branch['database_id']})
        identifier = branch['database_id']
        ready = sbx.wait('Upstash branch', lambda: self.ready(identifier, name, expected_root), 150)
        if ready.get('budget') != int(budget):
            self.request('/redis/update-budget/' + quote(identifier, safe=''), 'PATCH', {'budget': int(budget)})
        sbx.write_outputs(connection(ready))


if __name__ == '__main__':
    sbx.main(lambda action: UpstashBranch().run(action))
