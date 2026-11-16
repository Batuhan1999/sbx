#!/usr/bin/env python3
"""An OpenRouter API key per sandbox, with a credit limit, made with the
account's management key. OpenRouter shows a key's value only when it is
created, so the saved value is the key."""
import math
from urllib.parse import quote, urlencode

import sbx_provider as sbx

BASE_URL = 'https://openrouter.ai/api/v1'
ANTHROPIC_BASE_URL = 'https://openrouter.ai/api'
RESETS = ('daily', 'weekly', 'monthly')


def settings():
    """The key's limit settings, as OpenRouter names them, from the inputs."""
    try:
        limit = float(sbx.option('limit'))
    except ValueError:
        limit = math.nan
    if not 0 < limit < 1_000_000:
        raise sbx.ProviderError('set limit to each sandbox key\'s credit limit in USD, a number above 0')
    reset = sbx.option('limit_reset').lower()
    if reset not in ('', *RESETS):
        raise sbx.ProviderError('limit_reset is blank (no reset), daily, weekly or monthly')
    byok = sbx.option('include_byok_in_limit', 'true').lower()
    if byok not in ('true', 'false'):
        raise sbx.ProviderError('include_byok_in_limit is true or false')
    return {'limit': limit, 'limit_reset': reset or None, 'include_byok_in_limit': byok == 'true'}


class OpenRouter(sbx.Api):
    def __init__(self):
        super().__init__('OpenRouter', BASE_URL + '/keys',
                         {'Authorization': 'Bearer ' + sbx.env('OPENROUTER_MANAGEMENT_KEY')},
                         hint='check that the management key is read/write and has not expired')

    def keys(self):
        """Every API key of the account's workspace, disabled ones included."""
        def page(offset):
            found = self.request('?' + urlencode({'include_disabled': 'true', 'offset': offset}))['data']
            return found, offset + len(found) if isinstance(found, list) and found else None
        return list({k['hash']: k for k in sbx.all_pages('OpenRouter', page, 0)}.values())

    def path(self, key_hash):
        return '/' + quote(key_hash, safe='')

    def run(self, action):
        if action == 'list':
            print('\n'.join(k['name'] for k in self.keys() if sbx.owned(k.get('name'))))
            return
        name = sbx.resource_name()
        saved = sbx.previous('OpenRouter key', 'hash', 'key')
        if bool(saved.get('hash')) != bool(saved.get('key')):
            raise sbx.ProviderError('invalid saved OpenRouter key state; no key was changed')
        if action == 'delete':
            self.delete(name, saved.get('hash'))
        else:
            self.create(name, saved, settings())

    def create(self, name, saved, wanted):
        if saved.get('hash'):
            found = (self.request(self.path(saved['hash']), missing_ok=True) or {}).get('data')
            sbx.recorded('OpenRouter key', found and found['hash'], saved['hash'], creating=True)
            if found['name'] != name:
                raise sbx.ProviderError('the recorded OpenRouter key was renamed; no key was changed')
            key = saved['key']
        else:
            for stale in [k for k in self.keys() if k.get('name') == name]:
                # Made by a create that stopped before it saved the value, which cannot be read again.
                self.request(self.path(stale['hash']), 'DELETE', missing_ok=True)
            made = self.request('', 'POST', {'name': name, **wanted})
            key, found = made['key'], made['data']
            if not isinstance(key, str) or not key or found['name'] != name:
                raise sbx.ProviderError('OpenRouter returned no value for the new key')
        # Together and at once: a retry needs both, and the value is never shown again.
        sbx.write_outputs({'key': key, 'hash': found['hash']})
        if any(found.get(setting) != value for setting, value in wanted.items()):
            found = self.request(self.path(found['hash']), 'PATCH', wanted)['data']
            if any(found.get(setting) != value for setting, value in wanted.items()):
                raise sbx.ProviderError('OpenRouter did not apply the key\'s limit settings')
        sbx.write_outputs({'base_url': BASE_URL, 'anthropic_base_url': ANTHROPIC_BASE_URL})

    def delete(self, name, saved_hash):
        """The recorded key, or with none recorded the one key this resource's
        name may have left behind; never a same-named replacement."""
        hashes = [saved_hash] if saved_hash else [k['hash'] for k in self.keys() if k.get('name') == name]
        for key_hash in hashes:
            self.request(self.path(key_hash), 'DELETE', missing_ok=True)


if __name__ == '__main__':
    sbx.main(lambda action: OpenRouter().run(action))
