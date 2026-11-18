#!/usr/bin/env python3
"""Per-resource ClickHouse databases with schema-only or seeded table clones."""
import base64
import json
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import sbx_provider as sbx

# A database and its user share the name; SQL identifiers take no hyphens unquoted.
SEPARATOR = '_'


def identifier(value):
    return '`' + value.replace('\\', '\\\\').replace('`', '\\`') + '`'


def run(action):
    url = sbx.env('CH_URL')
    endpoint = urlsplit(url)
    if endpoint.scheme not in ('http', 'https') or not endpoint.hostname or endpoint.fragment:
        raise sbx.ProviderError('ClickHouse URL must be an HTTP(S) endpoint')
    if endpoint.scheme == 'http' and not sbx.private_host(endpoint.hostname):
        raise sbx.ProviderError('ClickHouse URL must use https:// unless it reaches a private address: '
                                'plain HTTP would send the password unencrypted')
    password = sbx.env('CH_PASSWORD', '')
    credentials = base64.b64encode(f"{sbx.env('CH_USER')}:{password}".encode()).decode()

    def query(sql):
        return sbx.fetch('ClickHouse', url, 'POST', {'Authorization': 'Basic ' + credentials}, sql.encode(), timeout=30,
                         hint='check the server permissions and template compatibility', redirects=False).decode()

    if action == 'list':
        print(query(f"SELECT name FROM system.databases WHERE startsWith(name, '{sbx.PREFIX}{SEPARATOR}') "
                    f"UNION DISTINCT SELECT name FROM system.users WHERE startsWith(name, '{sbx.PREFIX}{SEPARATOR}') "
                    "FORMAT TSVRaw"), end='')
        return
    database = sbx.resource_name(SEPARATOR)
    db = identifier(database)
    if action == 'delete':
        query(f'DROP DATABASE IF EXISTS {db} SYNC')
        query(f'DROP USER IF EXISTS {db}')
        return
    if len(password) < 16:
        raise sbx.ProviderError('a server password of at least 16 characters is required for stable sandbox credentials')
    mode, template = sbx.option('mode', 'schema'), sbx.option('template')
    if mode not in ('schema', 'clone'):
        raise sbx.ProviderError('ClickHouse template mode must be schema or clone')
    if mode == 'clone' and not template:
        raise sbx.ProviderError('clone mode requires a development template database')
    tables = []
    if template:
        # Hex keeps a supplied database name out of an SQL string literal.
        literal = f"unhex('{template.encode().hex()}')"
        if query(f'SELECT count() FROM system.databases WHERE name = {literal}').strip() != '1':
            raise sbx.ProviderError('the ClickHouse template database does not exist')
        tables = [json.loads(line) for line in query(f'SELECT name, engine FROM system.tables WHERE database = {literal} '
                                                    'AND NOT is_temporary FORMAT JSONEachRow').splitlines()]
        unsupported = [t['name'] for t in tables if t['engine'].startswith(('Replicated', 'Shared'))
                       or not (t['engine'].endswith('MergeTree') or mode == 'schema' and t['engine'] in ('Memory', 'Log', 'TinyLog', 'StripeLog'))]
        if unsupported:
            raise sbx.ProviderError('template contains unsupported views, replicated, or external engines: ' + ', '.join(unsupported))
    derived = sbx.derived_secret(password)
    query(f'CREATE DATABASE IF NOT EXISTS {db}')
    query(f"CREATE USER IF NOT EXISTS {db} IDENTIFIED WITH sha256_password BY '{derived}'")
    query(f"ALTER USER {db} IDENTIFIED WITH sha256_password BY '{derived}'")
    query(f'GRANT ALL ON {db}.* TO {db}')
    for table in tables:
        operation = 'CLONE AS' if mode == 'clone' else 'AS'
        query(f'CREATE TABLE IF NOT EXISTS {db}.{identifier(table["name"])} {operation} {identifier(template)}.{identifier(table["name"])}')
    host = endpoint.hostname
    port = endpoint.port or (443 if endpoint.scheme == 'https' else 80)
    authority = f'{quote(database, safe="")}:{derived}@{f"[{host}]" if ":" in host else host}:{port}'
    params = [(k, v) for k, v in parse_qsl(endpoint.query, keep_blank_values=True) if k != 'database']
    connection = urlunsplit((endpoint.scheme, authority, endpoint.path or '/', urlencode([*params, ('database', database)]), ''))
    sbx.write_outputs({'url': connection, 'http_url': connection, 'host': host, 'port': str(port), 'user': database,
                       'password': derived, 'database': database})


if __name__ == '__main__':
    sbx.main(run)
