"""The aws-role and aws-profile packages through the platform's generic
interfaces: a connection's connect and check code (against a fake STS), a
package's runtime layer (core.runtime_layer), and its action at the
sandbox action route."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from unittest.mock import ANY, Mock, patch

from support import Server

FOLDER = '/run/sbx/ext/aws-profile'


class AwsTests(Server):
    SETTINGS = {'repos': ['owner/awsapp']}

    def setUp(self):
        super().setUp()
        from sbx_control import catalog, settings, state
        from sbx_control.catalog import attachments, resources
        self.catalog, self.settings, self.state, self.attachments, self.resources = (
            catalog, settings, state, attachments, resources)

    def aws(self):
        """aws-profile's code as the platform loaded it: its session cache and errors."""
        return self.catalog.code(self.catalog.get('aws-profile'), 'aws.py')

    def sts_answers(self, **credentials):
        self.sts.assume_role.return_value = {'Credentials': {
            'AccessKeyId': 'fixture-aws-access', 'SecretAccessKey': 'fixture-aws-secret',
            'SessionToken': 'fixture-aws-session', 'Expiration': datetime.now(timezone.utc) + timedelta(minutes=15),
            **credentials}}

    def save(self, inputs, key='dev-aws'):
        return self.client.post('/api/catalog/connections', json={'package': 'aws-role', 'key': key, 'inputs': inputs})

    def aws_fixture(self, source=None):
        self.repo_admin()
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch('sbx_control.paths.apps', return_value=Path(directory)))
        recipe = Path(directory) / 'owner/awsapp/.sbx'
        recipe.mkdir(parents=True)
        (recipe / 'sbx.yaml').write_text('services:\n  aws:\n    uses: aws-profile\n'
            '    connections: {role: dev-aws}\n    with: {profile: team-dev}\n')
        self.sts = Mock()
        self.sts_answers()
        self.session = self.enterContext(patch('boto3.Session'))
        self.session.return_value.client.return_value = self.sts
        # The connection's own check assumes the role, against the fake STS.
        response = self.save({'role_arn': 'arn:aws:iam::123456789012:role/sbx-development',
                              'region': 'eu-central-1', 'source_profile': 'host-dev', 'external_id': 'fixture-external',
                              **(source or {})})
        self.assertEqual(response.status_code, 200, response.text)
        first = self.resources.create('aws-a', 'owner/awsapp', 'aws', 'aws-profile', {'profile': 'team-dev'}, 0, {'role': 'dev-aws'})
        second = self.resources.create('aws-b', 'owner/awsapp', 'aws', 'aws-profile', {'profile': 'team-dev'}, 0, {'role': 'dev-aws'})
        self.sts.assume_role.reset_mock()
        self.session.reset_mock()
        self.session.return_value.client.return_value = self.sts
        return first, second

    def aws_request(self, sandbox, token):
        return self.client.post(f'/api/sandbox/actions/{sandbox}/aws/credentials',
                                headers={'Authorization': 'Bearer ' + token})

    def aws_runtime(self, name, env=None):
        """(env, runtime layer) as a prepare writes them: live attachments
        reconciled first, then every contribution's runtime."""
        from sbx_control import core, recipes
        self.resources.reconcile_live(name, 'owner/awsapp')
        return core.runtime_layer(name, recipes.load('owner/awsapp'), dict(env or {}), True, {})

    def test_aws_scoped_refresh_and_cache_use_the_bound_role(self):
        first, _ = self.aws_fixture()
        response = self.aws_request('aws-a', first.outputs['token'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json()['Version'], 1)
        self.assertIn('Expiration', response.json())
        self.session.assert_called_once_with(profile_name='host-dev', region_name='eu-central-1')
        args = self.sts.assume_role.call_args.kwargs
        self.assertEqual(args['RoleArn'], 'arn:aws:iam::123456789012:role/sbx-development')
        self.assertEqual(args['ExternalId'], 'fixture-external')
        self.assertEqual(args['DurationSeconds'], 900)
        self.assertTrue(args['RoleSessionName'].startswith('sbx-aws-a-'))
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).json(), response.json())
        self.assertEqual(self.sts.assume_role.call_count, 1)

    def test_aws_tokens_are_private_per_sandbox_and_not_general_api_tokens(self):
        first, second = self.aws_fixture()
        self.assertNotEqual(first.outputs['token'], second.outputs['token'])
        self.assertEqual(self.aws_request('aws-b', first.outputs['token']).status_code, 401)
        self.assertEqual(self.aws_request('aws-a', 'wrong-token').status_code, 401)
        self.assertEqual(self.aws_request('absent', first.outputs['token']).status_code, 401)
        self.assertEqual(self.client.post('/api/sandbox/actions/aws-a/aws/credentials').status_code, 401)
        self.assertEqual(self.client.post('/api/sandbox/actions/aws-a/aws/delete',
                                          headers={'Authorization': 'Bearer ' + first.outputs['token']}).status_code, 401)
        self.sts.assume_role.assert_not_called()
        self.assertEqual(self.aws_request('aws-b', second.outputs['token']).status_code, 200)
        with self.state.db() as conn:
            public, sealed = conn.execute("select outputs,sensitive from sandbox_services where sandbox='aws-a'").fetchone()
        self.assertNotIn(first.outputs['token'], public + sealed)
        self.assertNotIn(first.outputs['token'], self.client.get('/api/repos/owner/awsapp/attachments').text)

    def test_aws_ready_retry_preserves_the_refresh_capability(self):
        first, _ = self.aws_fixture()
        again = self.resources.create('aws-a', 'owner/awsapp', 'aws', 'aws-profile', {}, 0)
        self.assertEqual(again.outputs, first.outputs)

    def test_aws_cached_session_is_not_returned_after_deletion(self):
        first, second = self.aws_fixture()
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 200)
        self.resources.delete_one('aws-a', 'aws')
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 401)
        self.assertEqual(self.aws_request('aws-b', second.outputs['token']).status_code, 200)

    def test_aws_untrusted_repo_or_detached_profile_cannot_refresh(self):
        first, _ = self.aws_fixture()
        self.settings.set_repos([], by='test')
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 401)
        self.settings.add_repo('owner/awsapp', by='test')
        self.attachments.detach('owner/awsapp', 'aws', by='test')
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 401)
        self.sts.assume_role.assert_not_called()

    def test_aws_session_renews_before_expiry_and_is_independent(self):
        first, second = self.aws_fixture()
        self.aws_request('aws-b', second.outputs['token'])
        self.sts_answers(Expiration=datetime.now(timezone.utc) + timedelta(seconds=60))
        for _ in range(2):
            self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 200)
        self.assertEqual(self.aws_request('aws-b', second.outputs['token']).status_code, 200)
        self.assertEqual(self.sts.assume_role.call_count, 3)

    def test_aws_denied_role_is_actionable_without_sdk_secret_output(self):
        from botocore.exceptions import ClientError
        first, _ = self.aws_fixture()
        self.sts.assume_role.side_effect = ClientError({'Error': {'Code': 'AccessDenied',
            'Message': 'fixture-private-source-credential'}}, 'AssumeRole')
        response = self.aws_request('aws-a', first.outputs['token'])
        self.assertEqual(response.status_code, 400)
        self.assertIn('AWS connection required', response.text)
        self.assertIn('AccessDenied', response.text)
        self.assertNotIn('fixture-private-source-credential', response.text)
        self.assertNotIn(first.outputs['token'], response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_aws_missing_host_profile_does_not_leak_process_stderr(self):
        from botocore.exceptions import CredentialRetrievalError
        first, _ = self.aws_fixture()
        self.session.side_effect = CredentialRetrievalError(provider='custom-process',
            error_msg='fixture-private-source-credential')
        response = self.aws_request('aws-a', first.outputs['token'])
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('fixture-private-source-credential', response.text)

    def test_aws_runtime_profile_is_generated_without_source_keys(self):
        first, _ = self.aws_fixture()
        injected = {'APP_VALUE': 'kept', 'DIRECT_DATABASE_URL': 'postgresql://sandbox-a/db-a',
                    'WEB_URL': 'https://sandbox-a.example.test'}
        env, layer = self.aws_runtime('aws-a', injected)
        self.assertEqual(env['AWS_PROFILE'], 'team-dev')
        self.assertEqual(env['AWS_CONFIG_FILE'], f'{FOLDER}/config')
        for key, value in injected.items():
            self.assertEqual(env[key], value)
        for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'):
            self.assertNotIn(name, env)
        files = {path: f.content for path, f in layer.files.items()}
        profile = files['aws-profile/config']
        self.assertIn('[profile team-dev]', profile)
        self.assertIn(f'credential_process = /usr/bin/python3 {FOLDER}/credential-process.py', profile)
        self.assertNotIn('sso_', profile)
        private = json.loads(files['aws-profile/aws/connection.json'])
        self.assertEqual(private['token'], first.outputs['token'])
        self.assertEqual(private['url'], 'https://203.0.113.10/api/sandbox/actions/aws-a/aws/credentials')
        for secret in ('fixture-aws-secret', 'fixture-aws-access', 'host-dev', 'fixture-external'):
            self.assertNotIn(secret, ''.join(files.values()))
        self.assertEqual(self.sts.assume_role.call_count, 1)

    def test_aws_runtime_preflight_refuses_conflicts_or_unavailable_auth(self):
        from botocore.exceptions import ProfileNotFound
        first, _ = self.aws_fixture()
        self.session.side_effect = ProfileNotFound(profile='host-dev')
        with self.assertRaises(ValueError):
            self.aws_runtime('aws-a')
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 400)
        self.session.side_effect = None
        for env in ({'AWS_ACCESS_KEY_ID': 'private'}, {'AWS_PROFILE': 'different'}):
            with self.assertRaises(ValueError):
                self.aws_runtime('aws-a', env)

    def test_aws_detached_profile_leaves_the_runtime(self):
        self.aws_fixture()
        self.attachments.detach('owner/awsapp', 'aws', by='test')
        self.resources.delete_one('aws-a', 'aws')
        env, layer = self.aws_runtime('aws-a', {'APP_VALUE': 'kept'})
        self.assertEqual({k: v for k, v in env.items() if k.startswith('AWS_')}, {})
        self.assertFalse([path for path in layer.files if path.startswith('aws-profile/')])

    def test_aws_prepare_can_add_a_profile_without_recreating_other_resources(self):
        _, second = self.aws_fixture()
        self.resources.delete_one('aws-a', 'aws')
        env, _ = self.aws_runtime('aws-a')
        self.assertEqual(env['AWS_PROFILE'], 'team-dev')
        self.assertEqual(env['AWS_REGION'], 'eu-central-1')
        self.assertEqual(self.aws_request('aws-b', second.outputs['token']).status_code, 200)

    def test_aws_runtime_restore_reuses_capability_and_connection(self):
        self.aws_fixture()
        _, first = self.aws_runtime('aws-a')
        _, again = self.aws_runtime('aws-a')
        self.assertEqual({p: f.content for p, f in first.files.items()}, {p: f.content for p, f in again.files.items()})
        self.assertEqual(self.sts.assume_role.call_count, 1)

    def test_aws_missing_identity_fails_before_allocating_a_vm(self):
        from botocore.exceptions import NoCredentialsError
        from sbx_control import core
        self.aws_fixture()
        self.session.side_effect = NoCredentialsError()
        with patch.object(core, 'find', return_value=None), patch.object(core.runtime, 'create') as create:
            with self.assertRaisesRegex(ValueError, 'AWS connection required'):
                core.create('aws-c', 'aws-c', 'operator@example.com', 'main', '',
                            'owner/awsapp', template='fixture')
            create.assert_not_called()

    def test_aws_cold_resume_restores_the_managed_profile(self):
        from sbx_control import core, platform
        self.aws_fixture()
        sandbox = Mock(sandbox_id='aws-a-vm')
        # Booted from its disk: the tmpfs is empty; its transport answers once asked.
        sandbox.commands.run.side_effect = lambda command, **kwargs: Mock(
            stdout='ready' if command == platform.STARTED else '')
        info = Mock(metadata={'sbx': 'aws-a', 'owner': 'operator@example.com', 'branch': 'main', 'repo': 'owner/awsapp'},
                    state='running')
        self.assertTrue(core.restore_runtime(sandbox, info))
        files = {c.args[0]: c.args[1] for c in sandbox.files.write.call_args_list}
        [profile] = [content for path, content in files.items() if path.endswith('/aws-profile/config')]
        self.assertIn('[profile team-dev]', profile)
        self.assertTrue(any(f'AWS_CONFIG_FILE={FOLDER}/config' in content for content in files.values()))
        self.assertNotIn('fixture-aws-secret', ''.join(files.values()))

    def test_aws_profile_rename_revokes_only_its_capability_on_prepare(self):
        first, second = self.aws_fixture()
        self.attachments.attach('owner/awsapp', 'aws-profile', 'aws', {'profile': 'renamed'}, {'role': 'dev-aws'}, by='test')
        env, _ = self.aws_runtime('aws-a', {'APP_VALUE': 'kept'})
        self.assertEqual(env['AWS_PROFILE'], 'renamed')
        self.assertEqual(env['APP_VALUE'], 'kept')
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 401)
        self.assertEqual(self.aws_request('aws-b', second.outputs['token']).status_code, 200)

    def test_aws_profile_rename_resolves_current_service_env_on_first_prepare(self):
        from sbx_control import core, platform
        first, second = self.aws_fixture()
        self.attachments.attach('owner/awsapp', 'aws-profile', 'aws', {'profile': 'renamed'}, {'role': 'dev-aws'}, by='test')
        info = Mock(metadata={'sbx': 'aws-a', 'repo': 'owner/awsapp', 'owner': 'owner@example.com'})
        with patch.object(platform, 'credentials', return_value=platform.Credentials('', {})):
            env, _, layer = core.runtime_values(info)
        self.assertEqual(env['AWS_PROFILE'], 'renamed')
        self.assertIn('[profile renamed]', layer.files['aws-profile/config'].content)
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 401)
        self.assertEqual(self.aws_request('aws-b', second.outputs['token']).status_code, 200)

    def test_aws_invalid_refresh_does_not_create_resource_lock_files(self):
        from sbx_control import paths
        self.aws_fixture()
        locks = paths.state_dir() / 'service-locks'
        before = sorted(locks.iterdir())
        for token in ('é' * 43, 'a' * 43, ''):
            with self.assertRaises(self.resources.Unauthorized):
                self.resources.action('arbitrary', 'arbitrary', 'credentials', token)
        self.assertEqual(sorted(locks.iterdir()), before)

    def test_aws_access_key_connection_refreshes_without_a_host_profile(self):
        source = {'auth_method': 'access_key', 'access_key_id': 'AKIAFIXTURE1234567890',
                  'secret_access_key': 'fixture-source-secret'}
        first, second = self.aws_fixture(source)
        self.sts_answers(Expiration=datetime.now(timezone.utc) + timedelta(seconds=60))
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 200)
        self.session.assert_called_once_with(botocore_session=ANY, aws_access_key_id=source['access_key_id'],
            aws_secret_access_key=source['secret_access_key'], aws_session_token=None, region_name='eu-central-1')
        self.assertEqual(self.aws_request('aws-b', second.outputs['token']).status_code, 200)
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 200)
        self.assertEqual(self.sts.assume_role.call_count, 3)
        _, layer = self.aws_runtime('aws-a', {'DATABASE_URL': 'postgresql://own/database'})
        files = ''.join(f.content for f in layer.files.values())
        for secret in source.values():
            if secret != 'access_key':
                self.assertNotIn(secret, files)
        self.assertNotIn('fixture-source-secret', self.client.get('/api/catalog/connections').text)
        self.assertNotIn(source['access_key_id'], self.client.get('/api/catalog/connections').text)

    def test_aws_incomplete_access_keys_never_fall_back_to_host_credentials(self):
        aws = self.aws()
        base = {'auth_method': 'access_key', 'role_arn': 'arn:aws:iam::123456789012:role/dev',
                'region': 'eu-central-1', 'source_profile': 'host-dev'}
        with patch('boto3.Session') as session:
            for credentials in ({}, {'access_key_id': 'fixture-id'}, {'secret_access_key': 'fixture-secret'},
                                {'access_key_id': 'ASIAFIXTURE1234567890', 'secret_access_key': 'fixture-secret'}):
                with self.assertRaises(aws.ConnectionRequired):
                    aws.assume({**base, **credentials}, 'test')
            session.assert_not_called()

    def test_aws_access_keys_are_sealed_and_never_come_back(self):
        from sbx_control import secret_store
        self.repo_admin()
        source = {'auth_method': 'access_key', 'role_arn': 'arn:aws:iam::123456789012:role/dev',
                  'region': 'eu-central-1', 'access_key_id': 'AKIAFIXTURE1234567890',
                  'secret_access_key': 'fixture-source-secret'}
        with patch('boto3.Session') as session:
            session.return_value.client.return_value.assume_role.return_value = {'Credentials': {
                'AccessKeyId': 'k', 'SecretAccessKey': 's', 'SessionToken': 't',
                'Expiration': datetime.now(timezone.utc) + timedelta(minutes=15)}}
            response = self.save(source, key='fixture-key-aws')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(source['access_key_id'], response.text)
        self.assertNotIn(source['secret_access_key'], response.text)
        with self.state.db() as conn:
            reference = conn.execute("select inputs from attachments where key = 'fixture-key-aws'").fetchone()[0]
        self.assertNotIn(source['secret_access_key'], reference)
        self.assertEqual(secret_store.unseal(reference)['secret_access_key'], source['secret_access_key'])

    def test_aws_source_key_rotation_keeps_resources_but_invalidates_cached_sessions(self):
        first, _ = self.aws_fixture({'auth_method': 'access_key', 'access_key_id': 'AKIAFIXTURE1234567890',
                                     'secret_access_key': 'fixture-source-one'})
        self.aws_request('aws-a', first.outputs['token'])
        response = self.save({'access_key_id': 'AKIAFIXTURE0987654321', 'secret_access_key': 'fixture-source-two'})
        self.assertEqual(response.status_code, 200, response.text)
        self.sts.assume_role.reset_mock()
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 200)
        self.assertEqual(self.sts.assume_role.call_count, 1)
        self.assertEqual(self.session.call_args.kwargs['aws_secret_access_key'], 'fixture-source-two')
        self.assertEqual(self.save({'role_arn': 'arn:aws:iam::123456789012:role/different'}).status_code, 400)

    def test_aws_live_host_connection_can_switch_to_saved_keys_without_recreating_resources(self):
        first, _ = self.aws_fixture()
        result = self.save({'auth_method': 'access_key', 'access_key_id': 'AKIAFIXTURE1234567890',
                            'secret_access_key': 'fixture-source-secret'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(self.attachments.connection('dev-aws')['inputs'].get('source_profile'))
        self.assertEqual(self.aws_request('aws-a', first.outputs['token']).status_code, 200)
        self.assertNotIn('profile_name', self.session.call_args.kwargs)

    def test_aws_rejected_access_key_update_does_not_replace_the_saved_connection(self):
        from botocore.exceptions import ClientError
        self.aws_fixture({'auth_method': 'access_key', 'access_key_id': 'AKIAFIXTURE1234567890',
                          'secret_access_key': 'fixture-source-one'})
        self.sts.assume_role.side_effect = ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'no'}}, 'AssumeRole')
        result = self.save({'secret_access_key': 'fixture-source-rejected'})
        self.assertEqual(result.status_code, 400)
        self.assertNotIn('fixture-source-rejected', result.text)
        self.assertEqual(self.attachments.connection('dev-aws')['inputs']['secret_access_key'], 'fixture-source-one')

    def test_aws_role_check_runs_the_packages_own_code(self):
        with patch('boto3.Session') as session:
            session.return_value.client.return_value.assume_role.return_value = {'Credentials': {
                'AccessKeyId': 'k', 'SecretAccessKey': 's', 'SessionToken': 't',
                'Expiration': datetime.now(timezone.utc) + timedelta(minutes=15)}}
            ok = self.catalog.check(self.catalog.get('aws-role'), {'role_arn': 'arn:aws:iam::123456789012:role/dev',
                                                                   'region': 'eu-central-1', 'source_profile': 'host-dev'})
        self.assertEqual(ok, '')
        self.assertIn('role ARN', self.catalog.check(self.catalog.get('aws-role'), {'role_arn': 'x', 'region': 'eu-central-1'}))

    def test_aws_saved_keys_ignore_an_unavailable_ambient_host_profile(self):
        import os
        source = {'auth_method': 'access_key', 'access_key_id': 'AKIAFIXTURE1234567890',
                  'secret_access_key': 'fixture-source-secret', 'region': 'eu-central-1',
                  'role_arn': 'arn:aws:iam::123456789012:role/dev'}
        returned = {'Credentials': {'AccessKeyId': 'fixture-issued-key', 'SecretAccessKey': 'fixture-issued-secret',
                    'SessionToken': 'fixture-issued-token', 'Expiration': datetime.now(timezone.utc) + timedelta(minutes=15)}}
        with patch.dict(os.environ, {'AWS_PROFILE': 'nonexistent-sbx-fixture', 'AWS_CONFIG_FILE': '/nonexistent-sbx-config',
                                    'AWS_SHARED_CREDENTIALS_FILE': '/nonexistent-sbx-credentials'}), \
                patch('botocore.client.BaseClient._make_api_call', return_value=returned):
            result = self.aws().assume(source, 'fixture-test')
        self.assertEqual(result['AccessKeyId'], 'fixture-issued-key')
