#!/usr/bin/env python3
"""Separate Upstash Redis databases for sandbox-owned development state."""
import base64
import re
from urllib.parse import quote

import sbx_provider as sbx

PLANS = {'free', 'payg', 'fixed_250mb', 'fixed_1gb', 'fixed_5gb', 'fixed_10gb',
         'fixed_50gb', 'fixed_100gb', 'fixed_500gb'}
REGIONS = {
    'aws': {'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2', 'ca-central-1',
            'eu-central-1', 'eu-west-1', 'eu-west-2', 'sa-east-1', 'ap-south-1',
            'ap-northeast-1', 'ap-southeast-1', 'ap-southeast-2', 'af-south-1'},
    'gcp': {'us-central1', 'us-east4', 'europe-west1', 'asia-northeast1'},
}
HOST_LABEL = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?')


class Upstash(sbx.Api):
    def __init__(self):
        credential = base64.b64encode((sbx.env('UPSTASH_EMAIL') + ':' + sbx.env('UPSTASH_API_KEY')).encode()).decode()
        super().__init__('Upstash', 'https://api.upstash.com/v2', {'Authorization': 'Basic ' + credential},
                         hint='check account access, quota, region and plan')

    def databases(self):
        databases = self.request('/redis/databases')
        if not isinstance(databases, list):
            raise sbx.ProviderError('Upstash returned an unexpected database list; no database was changed')
        return databases

    def active(self, identifier, name, region):
        database = self.request('/redis/database/' + quote(identifier, safe=''))
        if database.get('database_name') != name or database.get('database_id') != identifier:
            raise sbx.ProviderError('Upstash database identity changed; no connection values were published')
        if database.get('primary_region') not in (None, region):
            raise sbx.ProviderError('the existing database uses a different region; remove it before replacing its configuration')
        if database.get('state') == 'suspended':
            raise sbx.ProviderError('the Upstash database is suspended; resolve account or budget limits before retrying')
        return database if database.get('state') == 'active' else None

    def run(self, action):
        if action == 'list':
            print('\n'.join(d['database_name'] for d in self.databases() if sbx.owned(d.get('database_name'))))
            return
        name = sbx.resource_name()
        saved_id = sbx.previous('Upstash database', 'database_id').get('database_id')
        database = sbx.only(self.databases(), 'Upstash resource name', lambda d: d.get('database_name') == name)
        sbx.recorded('Upstash database', database and database.get('database_id'), saved_id, creating=action == 'create')
        if action == 'delete':
            if database:
                self.request('/redis/database/' + quote(database['database_id'], safe=''), 'DELETE', missing_ok=True)
            return
        platform, region = sbx.option('platform', 'aws'), sbx.option('region')
        plan, budget = sbx.option('plan', 'free'), sbx.option('budget', '1')
        if platform not in REGIONS or region not in REGIONS[platform]:
            raise sbx.ProviderError('choose a supported Upstash platform and matching primary region')
        if plan not in PLANS or not re.fullmatch(r'[1-9][0-9]*', budget):
            raise sbx.ProviderError('choose a supported Upstash plan and a positive integer monthly budget')
        if not database:
            database = self.request('/redis/database', 'POST', {
                'database_name': name, 'platform': platform, 'primary_region': region,
                'plan': plan, 'budget': int(budget), 'tls': True, 'eviction': False,
            })
        identifier = database['database_id']
        sbx.write_outputs(connection(sbx.wait('Upstash database', lambda: self.active(identifier, name, region), 90)))


def connection(database):
    if database.get('tls') is not True:
        raise sbx.ProviderError('Upstash database must have TLS enabled; no credentials were published')
    password, token = database.get('password'), database.get('rest_token')
    if not isinstance(password, str) or not password or not isinstance(token, str) or not token:
        raise sbx.ProviderError('Upstash withheld database credentials; check credential protection and Developer API permissions')
    host = database.get('endpoint', '')
    if not isinstance(host, str) or not host or len(host) > 253 or not all(HOST_LABEL.fullmatch(label) for label in host.split('.')):
        raise sbx.ProviderError('Upstash returned an invalid endpoint; no credentials were published')
    if '.' not in host:
        host += '.upstash.io'
    try:
        port = int(database.get('port'))
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise sbx.ProviderError('Upstash returned an invalid Redis port')
    return {'url': f'rediss://default:{quote(password, safe="")}@{host}:{port}/0',
            'rest_url': 'https://' + host, 'rest_token': token, 'host': host, 'port': str(port),
            'user': 'default', 'password': password, 'database': '0', 'database_id': database['database_id']}


if __name__ == '__main__':
    sbx.main(lambda action: Upstash().run(action))
