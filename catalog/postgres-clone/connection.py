#!/usr/bin/env python3
"""The sandbox's database and login name, the login's password (checking
PG_ADMIN_URL first), or with write its connection on the admin URL's host,
port and options."""
from urllib.parse import quote, urlsplit, urlunsplit

import sbx_provider as sbx

# The database and its login share the name; unquoted SQL identifiers take no hyphens.
SEPARATOR = '_'


def admin_endpoint():
    endpoint = urlsplit(sbx.env('PG_ADMIN_URL'))
    try:
        port = endpoint.port or 5432
    except ValueError:
        port = None
    if endpoint.scheme not in ('postgres', 'postgresql') or not endpoint.hostname or endpoint.fragment or not port:
        raise sbx.ProviderError('invalid PostgreSQL connection URL; check its scheme, host and port')
    return endpoint, port


def password():
    admin_endpoint()
    return sbx.derived_secret(sbx.env('PG_ADMIN_URL'))


def connection(database, password):
    endpoint, port = admin_endpoint()
    host = endpoint.hostname
    authority = f'{quote(database, safe="")}:{quote(password, safe="")}@{f"[{host}]" if ":" in host else host}:{port}'
    return {
        'url': urlunsplit((endpoint.scheme, authority, '/' + quote(database, safe=''), endpoint.query, '')),
        'password': password, 'user': database, 'database': database, 'host': host, 'port': str(port),
    }


def run(action):
    if action == 'name':
        print(sbx.resource_name(SEPARATOR))
    elif action == 'password':
        print(password())
    else:
        sbx.write_outputs(connection(sbx.resource_name(SEPARATOR), password()))


if __name__ == '__main__':
    sbx.main(run, ('name', 'password', 'write'))
