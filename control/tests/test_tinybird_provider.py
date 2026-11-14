"""Tinybird Forward branch contracts from the official Python SDK endpoints."""
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from catalog_scripts import ScriptCase, load, sbx_provider


class TinybirdProviderTests(ScriptCase):
    environment = {'TINYBIRD_URL': 'https://api.tinybird.test', 'TINYBIRD_WORKSPACE': 'parent-id',
                   'TINYBIRD_ADMIN_TOKEN': 'fixture-admin-token', 'INPUT_DATA': 'schema'}

    def setUp(self):
        super().setUp()
        self.module = load('tinybird-branch')
        self.calls, self.branch = [], None
        self.workspace = {'id': 'parent-id', 'name': 'development', 'main': None}
        self.job = {'status': 'done'}
        self.enterContext(patch.object(sbx_provider, 'urlopen', side_effect=self.transport))
        self.enterContext(patch('time.sleep'))
        self.api = self.module.Tinybird()

    def transport(self, request, timeout):
        parsed = urlsplit(request.full_url)
        action, path, params = request.get_method(), parsed.path, parse_qs(parsed.query)
        self.calls.append((action, path, params))
        self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-admin-token')
        if path == '/v1/workspace':
            result = self.workspace
        elif path == '/v1/environments' and action == 'POST':
            self.branch = {'id': 'child-id', 'name': params['name'][0], 'token': 'fixture-child-token',
                           'data': params.get('data')}
            result = {'job': {'id': 'creation-job'}}
        elif path == '/v1/environments':
            result = {'environments': [self.workspace, *([self.branch] if self.branch else [])]}
        elif path == '/v0/jobs/creation-job':
            result = self.job
        elif path == '/v0/environments/child-id' and action == 'DELETE':
            self.branch = None
            return io.BytesIO(b'')
        elif path == '/v0/environments/sbx_audit_resource' and self.branch:
            result = self.branch
        else:
            raise HTTPError(request.full_url, 404, 'missing', {}, io.BytesIO(b'fixture-private-response'))
        return io.BytesIO(json.dumps(result).encode())

    def test_schema_create_waits_for_job_and_publishes_only_branch_token(self):
        self.api.run('create')
        self.assertEqual((self.branch['name'], self.branch['data']), ('sbx_audit_resource', None))
        values = self.values()
        self.assertEqual(values['token'], 'fixture-child-token')
        self.assertEqual(values['workspace'], 'child-id')
        self.assertNotIn('fixture-admin-token', self.output.read_text())

    def test_last_partition_requires_explicit_selection(self):
        with patch.dict(os.environ, {'INPUT_DATA': 'last_partition'}):
            self.api.run('create')
        self.assertEqual(self.branch['data'], ['last_partition'])

    def test_retry_preserves_existing_branch_and_token(self):
        self.api.run('create')
        original = self.values()
        for status in ('creating', 'ready'):
            self.calls.clear()
            self.output.unlink()
            with self.subTest(status=status), patch.dict(os.environ, {
                    'SBX_PREVIOUS_OUTPUT': json.dumps(original), 'SBX_RESOURCE_STATUS': status}):
                self.api.run('create')
                self.assertEqual(self.values(), original)
                self.assertFalse(any(action in ('POST', 'DELETE') for action, _, _ in self.calls))

    def test_create_conflict_without_checkpoint_requires_explicit_recovery(self):
        original = self.transport
        def concurrent(request, timeout):
            if request.get_method() == 'POST':
                self.calls.append(('POST', '/v1/environments', {}))
                self.branch = {'id': 'child-id', 'name': 'sbx_audit_resource', 'token': 'fixture-child-token'}
                raise HTTPError(request.full_url, 409, 'already exists', {}, io.BytesIO(b''))
            return original(request, timeout)
        with patch.object(sbx_provider, 'urlopen', side_effect=concurrent):
            with self.assertRaisesRegex(ValueError, 'readiness is unknown'):
                self.api.run('create')
        self.assertFalse(self.output.exists())
        self.assertEqual(sum(action == 'POST' for action, _, _ in self.calls), 1)

    def test_same_name_replacement_is_not_adopted_or_deleted(self):
        self.branch = {'id': 'replacement-id', 'name': 'sbx_audit_resource', 'token': 'replacement-token'}
        for action in ('create', 'delete'):
            with self.subTest(action=action), patch.dict(os.environ, {
                'SBX_PREVIOUS_OUTPUT': json.dumps({'workspace': 'child-id'}), 'SBX_RESOURCE_STATUS': 'ready',
            }), self.assertRaisesRegex(ValueError, 'branch was replaced'):
                self.api.run(action)
        self.assertFalse(any(action in ('POST', 'DELETE') for action, _, _ in self.calls))
        self.assertFalse(self.output.exists())

    def test_missing_recorded_branch_is_not_recreated_and_delete_is_idempotent(self):
        with patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': json.dumps({'workspace': 'child-id'})}):
            with self.assertRaisesRegex(ValueError, 'branch is missing'):
                self.api.run('create')
            self.api.run('delete')
        self.assertFalse(any(action in ('POST', 'DELETE') for action, _, _ in self.calls))
        self.assertFalse(self.output.exists())

    def test_invalid_saved_identity_is_rejected_without_mutation(self):
        for previous in ('[]', '{invalid', '{"workspace": 42}', '{"workspace": ""}'):
            with self.subTest(previous=previous), patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': previous}):
                with self.assertRaisesRegex(ValueError, 'invalid saved Tinybird'):
                    self.api.run('delete')
        self.assertFalse(any(action in ('POST', 'DELETE') for action, _, _ in self.calls))

    def test_delete_uses_child_id_and_tolerates_already_absent_branch(self):
        self.api.run('create')
        self.calls.clear()
        self.api.run('delete')
        self.api.run('delete')
        self.assertEqual([call for call in self.calls if call[0] == 'DELETE'], [('DELETE', '/v0/environments/child-id', {})])
        self.assertIsNone(self.branch)

    def test_branch_token_cannot_be_used_as_parent_connection(self):
        self.workspace['main'] = 'real-parent'
        with self.assertRaisesRegex(ValueError, 'main workspace'):
            self.api.run('create')
        self.assertFalse(any(action == 'POST' for action, _, _ in self.calls))

    def test_wrong_workspace_and_parent_identity_are_not_changed(self):
        self.workspace['id'] = 'different-parent'
        with self.assertRaisesRegex(ValueError, 'selected main workspace'):
            self.api.run('delete')
        self.workspace['id'] = 'parent-id'
        self.branch = {'id': 'parent-id', 'name': 'sbx_audit_resource', 'token': 'fixture-token'}
        with self.assertRaisesRegex(ValueError, 'refusing to change'):
            self.api.run('delete')
        self.assertFalse(any(action == 'DELETE' for action, _, _ in self.calls))

    def test_failed_job_does_not_publish_credentials_or_raw_error(self):
        self.job = {'status': 'error', 'error': 'fixture-admin-token'}
        with self.assertRaisesRegex(ValueError, 'creation job failed') as error:
            self.api.run('create')
        self.assertNotIn('fixture-admin-token', str(error.exception))
        self.assertEqual(self.values(), {'_job_id': 'creation-job'})

    def test_failed_job_retry_does_not_accept_visible_branch_token_and_can_clean_up(self):
        self.job = {'status': 'error'}
        for attempt in range(2):
            if attempt:
                previous = self.values()
                self.output.unlink()
                self.enterContext(patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': json.dumps(previous)}))
            with self.assertRaisesRegex(ValueError, 'creation job failed'):
                self.api.run('create')
            self.assertEqual(self.values(), {'_job_id': 'creation-job'})
        self.assertEqual(sum(action == 'POST' for action, _, _ in self.calls), 1)
        self.api.run('delete')
        self.assertIsNone(self.branch)

    def test_pending_job_timeout_retry_waits_even_when_branch_token_is_visible(self):
        self.job = {'status': 'running'}
        for attempt in range(2):
            if attempt:
                previous = self.values()
                self.output.unlink()
                self.enterContext(patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': json.dumps(previous)}))
            with patch('time.monotonic', side_effect=[0, 151]):
                with self.assertRaisesRegex(ValueError, 'creation job is not ready'):
                    self.api.run('create')
            self.assertEqual(self.values(), {'_job_id': 'creation-job'})
        self.job = {'status': 'done'}
        self.output.unlink()
        self.calls.clear()
        self.api.run('create')
        self.assertEqual(self.values()['_job_complete'], 'true')
        self.assertEqual(self.values()['token'], 'fixture-child-token')
        self.assertEqual(sum(path == '/v0/jobs/creation-job' for _, path, _ in self.calls), 1)
        self.assertFalse(any(action == 'POST' for action, _, _ in self.calls))

    def test_existing_branch_without_ready_record_or_job_is_not_accepted(self):
        self.branch = {'id': 'child-id', 'name': 'sbx_audit_resource', 'token': 'fixture-child-token'}
        with self.assertRaisesRegex(ValueError, 'readiness is unknown'):
            self.api.run('create')
        self.assertFalse(self.output.exists())
        self.assertFalse(any(action in ('POST', 'DELETE') for action, _, _ in self.calls))

    def test_parent_admin_token_is_never_returned_as_child_token(self):
        self.branch = {'id': 'child-id', 'name': 'sbx_audit_resource', 'token': 'fixture-admin-token'}
        with patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': '{"workspace":"child-id"}', 'SBX_RESOURCE_STATUS': 'ready'}), self.assertRaisesRegex(ValueError, 'independent'):
            self.api.run('create')
        self.assertFalse(self.output.exists())

    def test_invalid_data_selection_makes_no_mutation(self):
        with patch.dict(os.environ, {'INPUT_DATA': 'all_production'}), self.assertRaisesRegex(ValueError, 'schema or last_partition'):
            self.api.run('create')
        self.assertFalse(any(action == 'POST' for action, _, _ in self.calls))

    def test_api_url_requires_https_without_embedded_credentials(self):
        for url in ('http://api.tinybird.test', 'https://admin:password@api.tinybird.test', 'https://api.tinybird.test/v1'):
            with self.subTest(url=url), patch.dict(os.environ, {'TINYBIRD_URL': url}):
                with self.assertRaisesRegex(ValueError, 'HTTPS API base URL'):
                    self.module.Tinybird()

    def test_list_outputs_owned_style_names_without_tokens(self):
        self.branch = {'id': 'child-id', 'name': 'sbx_audit_resource', 'token': 'fixture-child-token'}
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.api.run('list')
        self.assertEqual(output.getvalue(), 'sbx_audit_resource\n')


if __name__ == '__main__':
    unittest.main()
