"""Stable, authenticated preview origins for an installation with only an IP."""
import ipaddress
import re
import sys
import time
from urllib.parse import urlencode, urlsplit

from . import platform, runtime, settings, state

FIRST_PORT = 21000
LAST_PORT = 21255
STALE_GRACE_SECONDS = 300


class PortPoolExhausted(ValueError):
    pass


def port_range():
    network = settings.current().network
    return network.ip_preview_first_port, network.ip_preview_last_port


@settings.check("network")
def check_range(before, network):
    with state.db() as conn:
        if conn.execute('select 1 from ip_preview_routes where public_port < ? or public_port > ? limit 1',
                        (network.ip_preview_first_port, network.ip_preview_last_port)).fetchone():
            raise ValueError('IP preview range would exclude assigned URLs; delete their sandboxes before shrinking it')


def observe(sandbox_id, declared_ports, detected_ports):
    """Reclaim absent detected ports only after successful scans of this allocation."""
    now = time.time()
    live = set(declared_ports) | {p for p, loopback in detected_ports.items() if not loopback}
    removed = 0
    with state.db() as conn:
        conn.execute('begin immediate')
        row = conn.execute("select s.slug from sandboxes s join sandbox_lifecycle l on l.name = s.name "
                           "where json_extract(l.record, '$.sandbox_id') = ? "
                           "and json_extract(l.record, '$.phase') != 'deleted'", (sandbox_id,)).fetchone()
        if not row or not row[0]:
            return 0
        slug = row[0]
        routes = conn.execute('select app_port from ip_preview_routes where slug = ?', (slug,)).fetchall()
        for (port,) in routes:
            if port in live:
                conn.execute('delete from ip_preview_missing where slug = ? and app_port = ?', (slug, port))
                continue
            conn.execute('insert or ignore into ip_preview_missing values (?, ?, ?)', (slug, port, now))
            since = conn.execute('select since from ip_preview_missing where slug = ? and app_port = ?',
                                 (slug, port)).fetchone()[0]
            if now - since >= STALE_GRACE_SECONDS:
                conn.execute('delete from ip_preview_routes where slug = ? and app_port = ?', (slug, port))
                conn.execute('delete from ip_preview_missing where slug = ? and app_port = ?', (slug, port))
                removed += 1
        conn.execute('commit')
    return removed


def address():
    domain = settings.current().domain
    return domain.address if not settings.current().domain.name else ''


def host(value):
    ip = ipaddress.ip_address(value)
    return f'[{ip}]' if ip.version == 6 else str(ip)


def origin(slug, app_port):
    value = address()
    if not value:
        return ''
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', slug) or isinstance(app_port, bool) or not 1 <= app_port <= 65535:
        raise ValueError('Invalid sandbox preview')
    with state.db() as conn:
        conn.execute('begin immediate')
        if conn.execute('select 1 from ip_preview_retired where slug = ?', (slug,)).fetchone():
            conn.execute('commit')
            return ''
        row = conn.execute('select public_port from ip_preview_routes where slug = ? and app_port = ?',
                           (slug, app_port)).fetchone()
        if row is None:
            used = {r[0] for r in conn.execute('select public_port from ip_preview_routes')}
            first, last = port_range()
            port = next((p for p in range(first, last + 1) if p not in used), None)
            if port is None:
                raise PortPoolExhausted('All IP preview ports are in use; delete unused sandboxes or connect a domain')
            conn.execute('insert into ip_preview_routes (public_port, slug, app_port) values (?, ?, ?)',
                         (port, slug, app_port))
        else:
            port = row[0]
        conn.execute('commit')
    return f'https://{host(value)}:{port}'


def route(public_port):
    first, last = port_range()
    if not address() or not first <= public_port <= last:
        return None
    with state.db() as conn:
        return conn.execute('select slug, app_port from ip_preview_routes where public_port = ?',
                            (public_port,)).fetchone()


def remove(slug):
    with state.db() as conn:
        conn.execute('begin immediate')
        conn.execute('insert or ignore into ip_preview_retired (slug) values (?)', (slug,))
        conn.execute('delete from ip_preview_routes where slug = ?', (slug,))
        conn.execute('delete from ip_preview_missing where slug = ?', (slug,))
        conn.execute('commit')


def activate(slug):
    with state.db() as conn:
        conn.execute('delete from ip_preview_retired where slug = ?', (slug,))


def allowed_url(value):
    try:
        url = urlsplit(value)
        return bool(address() and url.scheme == 'https' and url.hostname == address() and
                    not url.username and not url.password and url.port and route(url.port) and
                    not any(c in value for c in ('\\', '\r', '\n', '\x00')))
    except ValueError:
        return False


def login_url(public_port, path):
    if not path.startswith('/') or any(c in path for c in ('\\', '\r', '\n', '\x00')):
        path = '/'
    target = f'https://{host(address())}:{public_port}{path}'
    return settings.current().base_url + '/auth/login?' + urlencode({'next': target})


def app_cookies(value):
    # Cookies are host-scoped, including across ports. The control cookie must
    # reach only the gate, even if a client sends duplicate cookie names.
    return '; '.join(part.strip() for part in value.split(';')
                     if part.partition('=')[0].strip() != platform.SESSION_COOKIE and part.strip())


def nginx(value, certificate):
    if not value:
        return ''
    ip = ipaddress.ip_address(value)
    if certificate not in ('bootstrap', str(ip)):
        raise ValueError('Invalid IP certificate name')
    cleared = "\n".join(f'    proxy_set_header {name} "";' for name in runtime.current().ingress_cleared)
    first, last = port_range()
    listeners = '\n'.join(f'  listen {"[::]:" if ip.version == 6 else ""}{p} ssl;' for p in range(first, last + 1))
    return f'''# IP previews share the control plane's renewable IP certificate.
map $ip_auth $ip_authorization {{ bearer ""; default $http_authorization; }}
map $http_upgrade $ip_connection {{ default upgrade; '' close; }}
server {{
{listeners}
  http2 on;
  server_name _;
  ssl_certificate /etc/letsencrypt/live/{certificate}/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/{certificate}/privkey.pem;
  client_max_body_size 0;
  location = /_sbx_ip_gate {{
    internal;
    proxy_pass http://127.0.0.1:{platform.CONTROL_PORT}/gate;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Host {host(str(ip))};
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Original-Host {host(str(ip))};
    proxy_set_header X-Original-Preview-Port $server_port;
    proxy_set_header X-Original-URI $request_uri;
    proxy_set_header X-Original-Method $ip_original_method;
    proxy_set_header Cookie $http_cookie;
    proxy_set_header Authorization $http_authorization;
  }}
  location @ip_login {{ return 302 $ip_login; }}
  location @ip_denied {{
    if ($ip_gone) {{ return 404; }}
    return 403;
  }}
  location / {{
    set $ip_original_method $request_method;
    auth_request /_sbx_ip_gate;
    auth_request_set $ip_upstream $upstream_http_x_sbx_upstream;
    auth_request_set $ip_cookies $upstream_http_x_sbx_cookies;
    auth_request_set $ip_auth $upstream_http_x_sbx_auth;
    auth_request_set $ip_login $upstream_http_x_sbx_login;
    auth_request_set $ip_gone $upstream_http_x_sbx_gone;
    error_page 401 = @ip_login;
    error_page 403 = @ip_denied;
    proxy_pass {runtime.current().ingress};
    proxy_http_version 1.1;
    proxy_set_header Host $ip_upstream;
{cleared}
    proxy_set_header X-Forwarded-Host {host(str(ip))}:$server_port;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header Cookie $ip_cookies;
    proxy_set_header Authorization $ip_authorization;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $ip_connection;
    proxy_buffering off;
    proxy_read_timeout 1d;
    proxy_send_timeout 1d;
  }}
}}
'''


SCHEMA = state.Schema(tables="""
create table if not exists ip_preview_routes (
    public_port integer primary key, slug text not null, app_port integer not null,
    unique (slug, app_port));
create table if not exists ip_preview_retired (slug text primary key);
create table if not exists ip_preview_missing (
    slug text not null, app_port integer not null, since real not null,
    primary key (slug, app_port));
""")


if __name__ == '__main__':
    if sys.argv[1:2] == ['nginx'] and len(sys.argv) == 4:
        print(nginx(sys.argv[2], sys.argv[3]), end='')
    else:
        raise SystemExit('usage: python -m sbx_control.ip_previews nginx ADDRESS CERTIFICATE')
