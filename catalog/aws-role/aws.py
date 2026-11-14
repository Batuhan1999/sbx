"""AWS development roles for sandboxes, without handing them the host's source
identity. Loaded by sbx-control (catalog.code): aws-role connections use
connect() and check(); each aws-profile service's sandbox gets a named
profile whose credential_process calls the credentials action, which assumes
the connection's role on the host and caches the session.

aws-profile links this file, so both packages share one copy."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import threading
from urllib.parse import urlsplit

PROFILE_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}')
REGION_RE = re.compile(r'[a-z]{2}(?:-[a-z0-9]+)+-\d+')
ROLE_RE = re.compile(r'arn:(aws|aws-cn|aws-us-gov):iam::\d{12}:role/[A-Za-z0-9_+=,.@/-]{1,512}')
KEY_RE = re.compile(r'[a-z0-9][a-z0-9_-]*')
CONFLICTS = {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN',
             'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_CONTAINER_CREDENTIALS_FULL_URI',
             'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI'}
_cache = {}
_cache_lock = threading.Lock()


class ConnectionRequired(ValueError):
    pass


def authentication(values):
    method = values.get('auth_method')
    if not method:
        method = 'access_key' if values.get('access_key_id') or values.get('secret_access_key') else (
            'host_profile' if values.get('source_profile') else 'access_key')
    if method not in ('access_key', 'host_profile'):
        raise ConnectionRequired('Choose access key or host profile authentication')
    return method


def connect(before, given):
    """A connection's inputs after an update: switching how it authenticates
    drops the other method's fields."""
    values = {**before, **given}
    method = authentication(values)
    values['auth_method'] = method
    if method == 'access_key':
        values.pop('source_profile', None)
    else:
        for field in ('access_key_id', 'secret_access_key', 'session_token'):
            values.pop(field, None)
    return values


def validate(values):
    if not ROLE_RE.fullmatch(values.get('role_arn', '')):
        raise ConnectionRequired('Set a valid AWS role ARN on the AWS connection')
    if not REGION_RE.fullmatch(values.get('region', '')):
        raise ConnectionRequired('Set a valid AWS region on the AWS connection')
    if authentication(values) == 'access_key':
        if not values.get('access_key_id', '').strip() or not values.get('secret_access_key', '').strip():
            raise ConnectionRequired('Enter both the AWS access key ID and secret access key')
        if values['access_key_id'].startswith('ASIA') and not values.get('session_token'):
            raise ConnectionRequired('Temporary AWS source credentials need a session token and expire; use a dedicated IAM access key for unattended access')
    elif not PROFILE_RE.fullmatch(values.get('source_profile') or 'default'):
        raise ConnectionRequired('Set a named AWS source profile on the sbx host')
    external = values.get('external_id', '')
    if external and not re.fullmatch(r'[A-Za-z0-9_+=,.@:/-]{2,1224}', external):
        raise ConnectionRequired('The AWS role external ID has an invalid format')


def assume(values, session_name):
    import boto3
    from botocore.session import Session
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    validate(values)
    try:
        if authentication(values) == 'access_key':
            source = Session()
            source.get_component('config_store').set_config_variable('profile', None)
            session = boto3.Session(botocore_session=source, aws_access_key_id=values['access_key_id'],
                                    aws_secret_access_key=values['secret_access_key'],
                                    aws_session_token=values.get('session_token') or None,
                                    region_name=values['region'])
        else:
            session = boto3.Session(profile_name=values.get('source_profile') or 'default', region_name=values['region'])
        client = session.client('sts', config=Config(connect_timeout=5, read_timeout=10,
                                retries={'mode': 'standard', 'total_max_attempts': 2},
                                ignore_configured_endpoint_urls=True))
        args = dict(RoleArn=values['role_arn'], RoleSessionName=session_name, DurationSeconds=900)
        if values.get('external_id'):
            args['ExternalId'] = values['external_id']
        response = client.assume_role(**args)['Credentials']
        return {'Version': 1, 'AccessKeyId': response['AccessKeyId'],
                'SecretAccessKey': response['SecretAccessKey'], 'SessionToken': response['SessionToken'],
                'Expiration': response['Expiration'].astimezone(timezone.utc).isoformat()}
    except (BotoCoreError, ClientError) as error:
        # SDK errors may contain credential-process stderr or request details.
        code = error.response.get('Error', {}).get('Code', '') if isinstance(error, ClientError) else type(error).__name__
        code = code if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) else 'AWSAuthError'
        remedy = ('check the saved access key, role trust and sts:AssumeRole permission'
                  if authentication(values) == 'access_key' else
                  'check the host profile, role trust and sts:AssumeRole permission; refresh an expired host SSO session')
        raise ConnectionRequired(f'AWS connection required ({code}): {remedy}') from None


def check(values):
    """An aws-role connection's check: can the host assume the role?"""
    try:
        assume(values, 'sbx-connection-check')
    except ConnectionRequired as error:
        return str(error)
    return ''


def _session(sandbox, key, token, values):
    validate(values)
    cache_key = (sandbox, key, hashlib.sha256((token + json.dumps(values, sort_keys=True)).encode()).hexdigest())
    now = datetime.now(timezone.utc)
    with _cache_lock:
        for expired in [k for k, v in _cache.items()
                        if datetime.fromisoformat(v['Expiration']) <= now + timedelta(minutes=5)]:
            del _cache[expired]
        found = _cache.get(cache_key)
    if found is None:
        session_name = re.sub(r'[^A-Za-z0-9_+=,.@-]', '-', f'sbx-{sandbox}')[:47] + '-' + cache_key[2][:12]
        found = assume(values, session_name)
        with _cache_lock:
            if len(_cache) >= 1024:
                _cache.pop(next(iter(_cache)))
            _cache[cache_key] = found
    return dict(found)


def credentials(context):
    """The credentials action: a fresh role session for the sandbox's profile.
    The binding is fixed at creation; a request never chooses its role."""
    return _session(context.sandbox, context.resource.key, context.token, context.resource.connections.get('role') or {})


def runtime(context):
    """One AWS config with a profile per aws-profile service, each resolving
    its credentials through credential-process.py and the credentials action.
    Fails before the sandbox's setup runs when a role can't be assumed."""
    env = dict(context.env)
    conflict = CONFLICTS & env.keys()
    if conflict:
        raise ConnectionRequired('Remove conflicting AWS credential inputs before using the managed profile: '
                                 + ', '.join(sorted(conflict)))
    url = urlsplit(context.base_url.rstrip('/'))
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path:
        raise ConnectionRequired('Managed AWS profiles require the control plane at a trusted HTTPS address')
    profiles, regions, files = {}, {}, {}
    for resource in context.resources:
        key, outputs = resource.key, resource.outputs
        profile = outputs.get('profile', '')
        if not PROFILE_RE.fullmatch(profile) or not KEY_RE.fullmatch(key):
            raise ConnectionRequired('Invalid managed AWS profile; recreate its attachment')
        if profile in profiles:
            raise ConnectionRequired(f'Multiple AWS attachments use profile {profile}; choose distinct profile names')
        _session(context.sandbox, key, outputs.get('token', ''), resource.connections.get('role') or {})
        profiles[profile] = (f'[profile {profile}]\n' if profile != 'default' else '[default]\n') + (
            f'credential_process = /usr/bin/python3 {context.folder}/credential-process.py '
            f'{context.folder}/{key}/connection.json\n'
            f'region = {outputs["region"]}\n')
        regions[profile] = outputs['region']
        files[f'{key}/connection.json'] = json.dumps({'url': context.action_url(key, 'credentials'),
                                                      'token': outputs['token']})
    selected = env.get('AWS_PROFILE') or next(iter(profiles))
    if selected not in profiles:
        raise ConnectionRequired(f'AWS_PROFILE {selected} has no managed attachment; match the repository profile name')
    files['credentials'] = ''
    files['credential-process.py'] = context.files['credential-process.py']
    files['config'] = '\n'.join(profiles.values())
    return {'env': {'AWS_PROFILE': selected, 'AWS_DEFAULT_PROFILE': selected,
                    'AWS_REGION': env.get('AWS_REGION') or regions[selected],
                    'AWS_DEFAULT_REGION': env.get('AWS_DEFAULT_REGION') or regions[selected],
                    'AWS_CONFIG_FILE': f'{context.folder}/config',
                    'AWS_SHARED_CREDENTIALS_FILE': f'{context.folder}/credentials', 'AWS_SDK_LOAD_CONFIG': '1',
                    'AWS_EC2_METADATA_DISABLED': 'true'},
            'files': files}
