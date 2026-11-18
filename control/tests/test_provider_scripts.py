"""Shipped provider contracts without requiring hosted accounts."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from catalog_scripts import ScriptCase, load, run_script, sbx_provider

ADMIN_URL = 'postgresql://admin:fixture@[2001:db8::1]:6543/postgres?sslmode=verify-full&connect_timeout=5'


class PostgresCloneTests(ScriptCase):
    environment = {'PG_ADMIN_URL': ADMIN_URL}

    def test_connections_preserve_tls_options_and_ipv6(self):
        output = load('postgres-clone', 'connection.py').connection('sbx_fixture', 'fixture-password')
        parsed = urlsplit(output['url'])
        self.assertEqual((parsed.hostname, parsed.port), ('2001:db8::1', 6543))
        self.assertEqual(parse_qs(parsed.query)['sslmode'], ['verify-full'])
        self.assertEqual(output['host'], '2001:db8::1')

    def test_invalid_admin_url_is_refused_without_echoing_it(self):
        for value in ('http://host/db', 'postgresql://', 'postgresql://user:fixture-secret@host:bad/db'):
            with self.subTest(value=value):
                result = run_script('postgres-clone', 'connection.py', {'PG_ADMIN_URL': value}, 'password')
                self.assertEqual(result.returncode, 1)
                self.assertIn('invalid PostgreSQL connection URL', result.stderr)
                self.assertNotIn('fixture-secret', result.stderr)

    def test_pause_ends_only_its_own_logins_idle_sessions(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (directory / 'psql').write_text('#!/bin/sh\necho "$@" > "$(dirname "$0")/args"; cat > "$(dirname "$0")/sql"\n')
        (directory / 'psql').chmod(0o755)
        result = run_script('postgres-clone', 'pause.sh', {'SBX_ID': 'abc-123', 'PATH': f'{directory}:{os.environ["PATH"]}'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('db=sbx_abc_123', (directory / 'args').read_text())
        # psql can't run here: the one guarantee is that only the sandbox's own login's sessions are ended.
        self.assertIn("datname = :'db' and usename = :'db'", (directory / 'sql').read_text())

    def test_scripts_get_the_sandbox_name_and_the_password_existing_logins_use(self):
        env = {'SBX_ID': 'abc-123', 'PG_ADMIN_URL': 'postgresql://admin:p@h:5432/postgres?sslmode=require'}
        name = run_script('postgres-clone', 'connection.py', env, 'name')
        password = run_script('postgres-clone', 'connection.py', env, 'password')
        self.assertEqual(name.stdout, 'sbx_abc_123\n', name.stderr)
        # The value `openssl dgst -sha256 -hmac "$PG_ADMIN_URL"` gave before, so retries keep the same login password.
        self.assertEqual(password.stdout, '322e3f802aa7ec449d1e98b6920e99f3ec7922be\n', password.stderr)
        written = run_script('postgres-clone', 'connection.py', {**env, 'SBX_OUTPUT': str(self.output)}, 'write')
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual({k: self.values()[k] for k in ('user', 'database', 'password', 'host', 'port')},
                         {'user': 'sbx_abc_123', 'database': 'sbx_abc_123', 'password': password.stdout.strip(),
                          'host': 'h', 'port': '5432'})


class ClickHouseTests(ScriptCase):
    environment = {'CH_USER': 'admin', 'CH_PASSWORD': 'fixture-password-long', 'INPUT_TEMPLATE': ''}

    def test_connections_preserve_https_ipv6_and_query_options(self):
        with patch.dict(os.environ, {'CH_URL': 'https://[2001:db8::2]:8443/service?session_timeout=10'}), \
                patch.object(sbx_provider, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(b'')):
            load('clickhouse-database').run('create')
        values = self.values()
        parsed = urlsplit(values['http_url'])
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.port, parsed.path), ('https', '2001:db8::2', 8443, '/service'))
        self.assertEqual(parse_qs(parsed.query)['database'], ['sbx_audit_resource'])
        self.assertEqual(parse_qs(parsed.query)['session_timeout'], ['10'])
        self.assertEqual(values['url'], values['http_url'])

    def test_plain_http_to_a_public_address_is_refused_before_any_request(self):
        provider = load('clickhouse-database')
        for url in ('http://8.8.8.8:8123', 'http://[2606:4700::1111]:8123', 'http://unresolvable.invalid:8123'):
            with self.subTest(url=url), patch.dict(os.environ, {'CH_URL': url}), \
                    patch.object(sbx_provider, 'urlopen') as query:
                with self.assertRaisesRegex(sbx_provider.ProviderError, 'must use https://'):
                    provider.run('create')
                query.assert_not_called()
        for url in ('http://127.0.0.1:8123', 'http://172.29.50.11:8123', 'http://100.101.1.2:8123', 'http://[fd00::5]:8123'):
            with self.subTest(url=url):
                self.assertTrue(sbx_provider.private_host(urlsplit(url).hostname))

    def test_shared_engines_and_views_are_refused_before_creating_resource(self):
        provider = load('clickhouse-database')
        for engine in ('ReplicatedMergeTree', 'SharedMergeTree', 'View', 'MaterializedView', 'PostgreSQL'):
            with self.subTest(engine=engine), patch.dict(os.environ, {'CH_URL': 'http://10.0.0.5:8123', 'INPUT_MODE': 'clone', 'INPUT_TEMPLATE': 'seed'}), \
                    patch.object(sbx_provider, 'urlopen', side_effect=[io.BytesIO(b'1\n'), io.BytesIO(json.dumps({'name': 'table', 'engine': engine}).encode())]) as query:
                with self.assertRaisesRegex(ValueError, 'unsupported'):
                    provider.run('create')
                self.assertEqual(query.call_count, 2)


class TemporalNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.binary = self.directory / 'temporal'
        self.env = {'PATH': f'{self.directory}:{os.environ["PATH"]}', 'INPUT_ADDRESS': 'fixture:7233', 'INPUT_RETENTION': '1d',
                    'SBX_ID': 'audit-resource', 'SBX_OUTPUT': str(self.directory / 'output')}

    def temporal(self, script):
        self.binary.write_text('#!/bin/sh\n' + script)
        self.binary.chmod(0o700)

    def test_create_names_the_namespace_and_accepts_an_existing_one(self):
        self.temporal('echo "$@" >> "$0.args"\necho NamespaceAlreadyExists >&2\nexit 1\n')
        result = run_script('temporal-namespace', 'create.sh', self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--namespace sbx-audit-resource', (self.directory / 'temporal.args').read_text())
        self.assertEqual((self.directory / 'output').read_text(), 'address=fixture:7233\nnamespace=sbx-audit-resource\n')

    def test_create_does_not_accept_unrelated_already_errors(self):
        self.temporal('echo "already shutting down" >&2\nexit 3\n')
        result = run_script('temporal-namespace', 'create.sh', self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.directory / 'output').exists())

    def test_list_empty_success_and_failure_are_distinct(self):
        self.temporal('printf \'[{"namespaceInfo": {"name": "sbx-audit-resource"}}, {"namespaceInfo": {"name": "default"}}]\\n\'\n')
        result = run_script('temporal-namespace', 'list.sh', self.env)
        self.assertEqual((result.returncode, result.stdout), (0, 'sbx-audit-resource\n'), result.stderr)
        self.temporal('echo fixture-unreachable >&2\nexit 3\n')
        result = run_script('temporal-namespace', 'list.sh', self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixture-unreachable', result.stderr)


class AwsProfileTests(ScriptCase):
    environment = {'AWS_PROFILE_REGION': 'eu-central-1', 'INPUT_PROFILE': 'dev'}

    def test_retry_keeps_the_refresh_token(self):
        create = load('aws-profile', 'create.py').create
        create('create')
        first = self.values()
        self.assertEqual((first['profile'], first['region']), ('dev', 'eu-central-1'))
        self.output.unlink()
        with patch.dict(os.environ, {'SBX_PREVIOUS_OUTPUT': json.dumps(first)}):
            create('create')
        self.assertEqual(self.values(), first)

    def test_invalid_profile_or_missing_region_publishes_nothing(self):
        create = load('aws-profile', 'create.py').create
        for env in ({'INPUT_PROFILE': '../escape'}, {'AWS_PROFILE_REGION': ''}):
            with self.subTest(env=env), patch.dict(os.environ, env), self.assertRaises(ValueError):
                create('create')
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
