#!/usr/bin/env python3
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse

PACKAGE = 'electric-source'
LABEL = 'sbx.package'
RESOURCE = 'sbx.resource'
ID = re.compile(r'[a-z][a-z0-9-]{1,30}')


def docker(*args, input=None, check=True):
    result = subprocess.run(['docker', *args], input=input, capture_output=True, text=True, timeout=240)
    if check and result.returncode:
        raise RuntimeError(f'docker {args[0]} failed (exit {result.returncode})')
    return result


action = os.environ['SBX_ACTION']
if action == 'list':
    result = docker('ps', '-a', '--filter', f'label={LABEL}={PACKAGE}', '--format', f'{{{{.Label "{RESOURCE}"}}}}')
    for resource in result.stdout.splitlines():
        if ID.fullmatch(resource):
            print(f'sbx-{resource}')
    raise SystemExit(0)
name = os.environ['SBX_ID']
if not ID.fullmatch(name):
    raise ValueError('invalid resource ID')
container = f'sbx-{name}'
found = docker('inspect', '--format', '{{json .Config.Labels}}', container, check=False)
if not found.returncode:
    labels = json.loads(found.stdout) or {}
    if labels.get(LABEL) != PACKAGE or labels.get(RESOURCE) != name:
        raise ValueError('refusing to touch a container outside this sandbox service')
if action == 'delete':
    if not found.returncode:
        docker('stop', '--time', '20', container)
        docker('rm', container)
    raise SystemExit(0)
url = os.environ['INPUT_DATABASE_URL']
password = urlparse(url).password
if not password:
    raise ValueError('the database connection needs a password')
secret = hmac.new(password.encode(), f'electric:{name}'.encode(), hashlib.sha256).hexdigest()
if found.returncode:
    env = {'DATABASE_URL': url + ('&' if '?' in url else '?') + 'sslmode=disable',
           'ELECTRIC_SECRET': secret, 'ELECTRIC_MANUAL_TABLE_PUBLISHING': 'true',
           'CLEANUP_REPLICATION_SLOTS_ON_SHUTDOWN': 'true',
           'ELECTRIC_TEMPORARY_REPLICATION_SLOT_USE_RANDOM_NAME': 'true',
           'ELECTRIC_DB_POOL_SIZE': '4', 'ELECTRIC_TELEMETRY': 'disabled'}
    docker('run', '-d', '--name', container, '--network', 'sbx-services', '--restart', 'unless-stopped',
           '--label', f'{LABEL}={PACKAGE}', '--label', f'{RESOURCE}={name}',
           '--memory', '768m', '--cpus', '1', '--env-file', '/dev/stdin', os.environ['INPUT_IMAGE'],
           input=''.join(f'{key}={value}\n' for key, value in env.items()))
else:
    docker('start', container)
address = docker('inspect', '--format', '{{(index .NetworkSettings.Networks "sbx-services").IPAddress}}', container).stdout.strip()
with Path(os.environ['SBX_OUTPUT']).open('a') as output:
    output.write(f'url=http://{address}:3000\nsecret={secret}\n')
