#!/usr/bin/env python3
"""Shared plumbing for catalog provider scripts. Standard library only.

The runner (control/sbx_control/catalog/resources.py) copies a package's folder to a
private directory and runs one script per action from there, so each package
that uses this module carries it as a symlink, `sbx_provider.py ->
../_lib/sbx_provider.py`; the catalog reads the link as a file of the package.

What a script gets and gives:
- the action (create, delete or list) as its first argument, else $SBX_ACTION;
- INPUT_<NAME> for each input, the exposed fields of its connections, SBX_ID (the
  resource ID), SBX_NAME, SBX_REPO, SBX_SERVICE, SBX_PREVIOUS_OUTPUT (a JSON
  object of what an earlier create wrote) and SBX_RESOURCE_STATUS (creating
  or ready);
- create appends key=value lines to $SBX_OUTPUT, list prints the names of the
  resources it can see, one per line, and a failure is one line on stderr
  with exit code 1.

Resource naming, the one contract every provider follows: a resource is named
`sbx-<id>`, with <id> the resource ID ([a-z][a-z0-9-]{1,30}). A provider whose
names may not contain hyphens (SQL identifiers, Tinybird branches) uses `_` as
the separator throughout: `sbx_<id with - as _>`.

Shell scripts get the name from `python3 ./sbx_provider.py name [-|_]`.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import sys
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

ACTIONS = ('create', 'delete', 'list')
PREFIX = 'sbx'
RESOURCE_ID = re.compile(r'[a-z][a-z0-9-]{1,30}')
OUTPUT_KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
USER_AGENT = 'sbx'


# Where plain HTTP is the admin's choice: loopback, RFC 1918, link-local, IPv6
# ULA, and shared address space (RFC 6598: Tailscale and other private overlays).
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10',
    '::1/128', 'fc00::/7', 'fe80::/10'))


def private_host(host):
    """Whether every address host resolves to is in PRIVATE_NETWORKS."""
    try:
        found = {ipaddress.ip_address(info[4][0].split('%')[0]) for info in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError, ValueError):
        return False
    found = {getattr(a, 'ipv4_mapped', None) or a for a in found}
    return bool(found) and all(any(a in network for network in PRIVATE_NETWORKS) for a in found)


class _RefuseRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None  # the 3xx surfaces as an HTTPError


def open_without_redirects(request, timeout):
    """urlopen that never follows a redirect: urllib would resend the request's
    Authorization header to the new location, which may be plain HTTP."""
    return build_opener(_RefuseRedirects).open(request, timeout=timeout)


class ProviderError(ValueError):
    """A failure whose message is safe to show: no credential or response body."""


class HttpError(ProviderError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None:
        raise ProviderError(f'{name} is not set; connect the connection this package requires')
    return value


def option(name, default=''):
    return os.environ.get('INPUT_' + name.upper(), '').strip() or default


def status():
    return os.environ.get('SBX_RESOURCE_STATUS', 'creating')


def resource_id():
    value = os.environ.get('SBX_ID', '')
    if not RESOURCE_ID.fullmatch(value):
        raise ProviderError('invalid service resource ID')
    return value


def resource_name(separator='-'):
    if separator not in ('-', '_'):
        raise ProviderError('the resource name separator is - or _')
    return PREFIX + separator + resource_id().replace('-', separator)


def derived_secret(key):
    """The resource's password, the same on every run without being stored:
    HMAC-SHA256 of the resource ID under a secret of the server it lives on."""
    return hmac.new(key.encode(), resource_id().encode(), hashlib.sha256).hexdigest()[:40]


def owned(name, separator='-'):
    return isinstance(name, str) and re.fullmatch(
        rf'{PREFIX}{re.escape(separator)}[a-z][a-z0-9{re.escape(separator)}]*', name) is not None


def previous(what, *identities):
    """The outputs an earlier create wrote. Each named identity is either
    absent or a non-empty string; anything else stops before any change."""
    try:
        value = json.loads(os.environ.get('SBX_PREVIOUS_OUTPUT') or '{}')
    except ValueError:
        value = None
    if not isinstance(value, dict) or any(
            value.get(key) is not None and (not isinstance(value[key], str) or not value[key]) for key in identities):
        raise ProviderError(f'invalid saved {what} state; no resource was changed')
    return value


def recorded(what, found_id, saved_id, creating=False):
    """Never adopt or delete a same-name replacement of the recorded resource,
    and never silently recreate a recorded resource that has gone."""
    if found_id is not None and saved_id and found_id != saved_id:
        raise ProviderError(f'the recorded {what} was replaced; no credentials or data were changed')
    if found_id is None and saved_id and creating:
        raise ProviderError(f'the recorded {what} is missing; recover it explicitly before replacing its state')


def only(items, what, match):
    found = [item for item in items if match(item)]
    if len(found) > 1:
        raise ProviderError(f'{what} is ambiguous; no resource was changed')
    return found[0] if found else None


def write_outputs(values):
    for key, value in values.items():
        if not OUTPUT_KEY.fullmatch(key) or not isinstance(value, str) or '\n' in value or '\r' in value:
            raise ProviderError(f'refusing to publish {key}: not a single-line text value')
    with open(env('SBX_OUTPUT'), 'a') as output:
        output.writelines(f'{key}={value}\n' for key, value in values.items())


def fetch(what, url, method='GET', headers=None, data=None, *, hint='check its access', timeout=20, missing_ok=False,
          redirects=True):
    """The response body, or None for a 404 when missing_ok. Errors name the
    status only: request headers and response bodies may carry credentials."""
    request = Request(url, data=data, method=method, headers={'User-Agent': USER_AGENT, **(headers or {})})
    try:
        with (urlopen if redirects else open_without_redirects)(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as error:
        error.close()
        if missing_ok and error.code == 404:
            return None
        raise HttpError(f'{what} {method} failed (HTTP {error.code}); {hint}', error.code) from None
    except OSError:  # URLError, timeouts and resets
        raise ProviderError(f'{what} did not answer; retry the same service to resume from its recorded resource') from None


class Api:
    """A JSON API under one base URL."""

    def __init__(self, what, base, headers=None, *, hint='check its access', timeout=20):
        self.what, self.base, self.headers, self.hint, self.timeout = what, base, dict(headers or {}), hint, timeout

    def request(self, path, method='GET', body=None, *, missing_ok=False):
        """The decoded JSON, or None for an empty body or, when missing_ok, a 404."""
        headers, data = dict(self.headers), None
        if body is not None:
            headers['Content-Type'], data = 'application/json', json.dumps(body).encode()
        elif method in ('POST', 'PUT', 'PATCH'):
            data = b''
        raw = fetch(self.what, self.base + path, method, headers, data, hint=self.hint, timeout=self.timeout,
                    missing_ok=missing_ok)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ProviderError(f'{self.what} returned a response that is not JSON') from None


def all_pages(what, page, first=None):
    """Every item of a paginated list. page(token) returns (items, next token or
    None). A repeated token stops, so an incomplete list is never acted on."""
    items, token, seen = [], first, {first}
    while True:
        batch, token = page(token)
        if not isinstance(batch, list):
            raise ProviderError(f'{what} returned an unexpected list; no resource was changed')
        items.extend(batch)
        if token is None:
            return items
        if token in seen:
            raise ProviderError(f'{what} repeated a pagination token; refusing to act on an incomplete list')
        seen.add(token)


def wait(what, check, timeout, interval=2):
    """check()'s first truthy result, polled every interval seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise ProviderError(f'{what} is not ready; retry the same service to resume provisioning')
        time.sleep(interval)


def main(run, actions=ACTIONS):
    """Run run(action) as the script's entry point; a single-action script
    needs no argument."""
    action = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('SBX_ACTION', actions[0] if len(actions) == 1 else '')
    try:
        if action not in actions:
            raise ProviderError(f'unsupported action {action!r}; expected one of {", ".join(actions)}')
        run(action)
    except ProviderError as error:
        sys.exit(str(error))
    except KeyError as error:
        sys.exit(f'the provider returned an incomplete response (no {error})')
    except (TypeError, ValueError):
        sys.exit('the provider returned an unexpected response; no connection values were published')


if __name__ == '__main__':
    def _cli(action):
        if action == 'name':
            print(resource_name(sys.argv[2] if len(sys.argv) > 2 else '-'))
    main(_cli, ('name',))
