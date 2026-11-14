#!/usr/bin/env python3
"""Tinybird Forward branches using the official SDK's environment endpoints."""
from urllib.parse import quote, urlencode, urlsplit

import sbx_provider as sbx

# Tinybird branch names allow letters, digits and underscores only.
SEPARATOR = '_'


class Tinybird(sbx.Api):
    def __init__(self):
        base = sbx.env('TINYBIRD_URL').rstrip('/')
        parsed = urlsplit(base)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or parsed.path:
            raise sbx.ProviderError('Tinybird requires a region HTTPS API base URL without a path or query')
        self.token, self.workspace = sbx.env('TINYBIRD_ADMIN_TOKEN'), sbx.env('TINYBIRD_WORKSPACE')
        if not self.workspace or not self.token or any(c in self.token for c in '\r\n'):
            raise sbx.ProviderError('Tinybird requires the selected main workspace ID and its admin token')
        super().__init__('Tinybird', base, {'Authorization': 'Bearer ' + self.token}, timeout=15,
                         hint='check Forward workspace access and branch state')

    def parent(self):
        workspace = self.request('/v1/workspace')
        if workspace.get('id') != self.workspace or workspace.get('main'):
            raise sbx.ProviderError('Tinybird token must belong to the selected main workspace, not a branch')
        return workspace

    def branch(self, name, saved_id):
        branch = self.request('/v0/environments/' + quote(name, safe='') + '?with_token=true', missing_ok=True)
        if branch is not None:
            if branch.get('name') != name or not isinstance(branch.get('id'), str) or not branch['id']:
                raise sbx.ProviderError('Tinybird returned a different branch; no resource was changed')
            if branch['id'] == self.workspace:
                raise sbx.ProviderError('refusing to change the Tinybird main workspace')
        sbx.recorded('Tinybird branch', branch and branch['id'], saved_id)
        return branch

    def with_token(self, name, saved_id):
        branch = self.branch(name, saved_id)
        return branch if branch and branch.get('token') else None

    def job_done(self, job_id):
        status = self.request('/v0/jobs/' + quote(job_id, safe='')).get('status')
        if status == 'error':
            raise sbx.ProviderError('Tinybird branch creation job failed; inspect the job in Tinybird before retrying')
        return status == 'done'

    def create_job(self, name, data):
        try:
            result = self.request('/v1/environments?' + urlencode({'name': name, **({'data': data} if data != 'schema' else {})}), 'POST')
        except sbx.HttpError as error:
            if error.status != 409:
                raise
            raise sbx.ProviderError('Tinybird branch readiness is unknown; recover the creation job explicitly before retrying') from None
        job_id = ((result or {}).get('job') or {}).get('id')
        if not isinstance(job_id, str) or not job_id or '\n' in job_id or '\r' in job_id:
            raise sbx.ProviderError('Tinybird returned no valid creation job ID; recover the recorded resource explicitly')
        return job_id

    def run(self, action):
        parent = self.parent()
        if action == 'list':
            body = self.request('/v1/environments')
            branches = body.get('environments')
            if not isinstance(branches, list) or body.get('next') or body.get('next_page'):
                raise sbx.ProviderError('Tinybird returned an unsupported branch list; refusing an incomplete inventory')
            print('\n'.join(b['name'] for b in branches if b.get('id') != self.workspace and sbx.owned(b.get('name'), SEPARATOR)))
            return
        name = sbx.resource_name(SEPARATOR)
        if name == parent.get('name'):
            raise sbx.ProviderError('refusing to change the Tinybird main workspace')
        previous = sbx.previous('Tinybird connection', 'workspace', '_job_id')
        saved_id = previous.get('workspace')
        branch = self.branch(name, saved_id)
        if action == 'delete':
            if branch:
                self.request('/v0/environments/' + quote(branch['id'], safe=''), 'DELETE', missing_ok=True)
            return
        sbx.recorded('Tinybird branch', branch and branch['id'], saved_id, creating=True)
        job_id, complete = previous.get('_job_id'), previous.get('_job_complete')
        if complete not in (None, 'true') or complete and not job_id:
            raise sbx.ProviderError('invalid saved Tinybird creation job state')
        data = sbx.option('data', 'schema')
        if data not in ('schema', 'last_partition'):
            raise sbx.ProviderError('Tinybird data must be schema or last_partition')
        if job_id or branch is None:
            # Recorded before waiting, so a retry resumes this job instead of creating another branch.
            job_id = job_id or self.create_job(name, data)
            sbx.write_outputs({'_job_id': job_id})
            if not complete:
                sbx.wait('Tinybird branch creation job', lambda: self.job_done(job_id), 120, interval=1)
            sbx.write_outputs({'_job_complete': 'true'})
            branch = None
        elif sbx.status() != 'ready' or not saved_id:
            raise sbx.ProviderError('Tinybird branch readiness is unknown; recover the creation job explicitly before retrying')
        if not (branch and branch.get('token')):
            branch = sbx.wait('Tinybird branch token', lambda: self.with_token(name, saved_id), 30, interval=1)
        token = branch['token']
        if not isinstance(token, str) or token == self.token or '\n' in token or '\r' in token:
            raise sbx.ProviderError('Tinybird returned no independent single-line branch token')
        sbx.write_outputs({'url': self.base, 'workspace': branch['id'], 'branch': name, 'token': token})


if __name__ == '__main__':
    sbx.main(lambda action: Tinybird().run(action))
