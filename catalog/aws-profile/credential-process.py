"""AWS credential_process: emit credentials only to the calling SDK, never stderr."""
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def main():
    try:
        connection = json.loads(Path(sys.argv[1]).read_text())
        url = urlsplit(connection['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('invalid credential endpoint')
        request = Request(connection['url'], data=b'', method='POST',
                          headers={'Authorization': 'Bearer ' + connection['token'], 'Accept': 'application/json'})
        with build_opener(NoRedirect).open(request, timeout=40) as response:
            result = json.loads(response.read(16384))
        if result.get('Version') != 1 or not all(isinstance(result.get(k), str) and result[k]
                for k in ('AccessKeyId', 'SecretAccessKey', 'SessionToken', 'Expiration')):
            raise ValueError('invalid credential response')
        if datetime.fromisoformat(result['Expiration']) <= datetime.now(timezone.utc):
            raise ValueError('expired credential response')
    except HTTPError as error:
        message = ('AWS sandbox access was revoked; prepare or recreate its attachment' if error.code == 401 else
                   'AWS connection required; check the role and source identity in sbx')
        print(message, file=sys.stderr)
        return 1
    except Exception:
        print('AWS credential refresh failed; check the sbx connection and trusted HTTPS access', file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
