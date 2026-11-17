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


# The CA bundle in nginx's image (alpine's ca-certificates): what an https
# ingress's certificate is verified against.
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def upstream_name(kind: str) -> str:
    """An nginx name for the kind's upstream, one per kind: lowercase letters
    and digits as they are, any other character as _<its hex code>."""
    return "sbx_runtime_" + "".join(c if c in "abcdefghijklmnopqrstuvwxyz0123456789" else f"_{ord(c):02x}"
                                    for c in kind)


def _upstream_url(kind: str, ingress: str) -> str:
    return f"{urlsplit(ingress).scheme or 'http'}://{upstream_name(kind)}"


def default_ingress() -> str:
    """The default runtime's upstream, for what reaches a runtime without the
    gate naming one (a CORS preflight); with no runtime set up, a closed port
    on this host (nginx answers 502: there is no sandbox to reach)."""
    found = runtime.ingresses()
    if not found:
        return NO_INGRESS
    kind, (ingress, _) = next(iter(found.items()))
    return _upstream_url(kind, ingress)


NO_INGRESS = "http://127.0.0.1:9"   # the discard port: nothing listens


def runtime_upstreams(source: str = "sbx_runtime", target: str = "sbx_ingress", cookie: str = "sandbox_cookie") -> str:
    """nginx: one upstream block per enabled runtime (its ingress) and a map
    from the kind the gate names (X-Sbx-Runtime, in $source) to it ($target);
    anything else goes to the default runtime's. For a runtime that wants
    its sandboxes' token in a header, a map that sends it to that runtime
    only; for one that wants it in a cookie, the request's cookies ($cookie,
    the app's own) with the token appended, for that runtime only
    ($<target>_cookie, upstream_cookie)."""
    found = runtime.ingresses()
    lines = ["# One upstream per enabled sandbox runtime; the gate names a sandbox's (X-Sbx-Runtime)."]
    for kind, (ingress, _) in found.items():
        parts = urlsplit(ingress)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        name = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
        lines.append(f"upstream {upstream_name(kind)} {{ server {name}:{port}; }}")
    lines.append(f"map ${source} ${target} {{")
    lines.append(f"  default {default_ingress()};")
    lines += [f"  {kind} {_upstream_url(kind, ingress)};" for kind, (ingress, _) in found.items()]
    lines.append("}")
    found_headers = runtime.upstream_token_headers()
    headers = sorted(set(found_headers.values()))
    if headers:
        lines.append("# The sandbox's token, in the header its runtime wants, to that runtime only.")
    for number, header in enumerate(headers, 1):
        lines.append(f"map ${source} ${target}_token_{number} {{")
        lines.append('  default "";')
        lines += [f"  {kind} ${target}_token;" for kind, h in found_headers.items() if h == header]
        lines.append("}")
    cookies = runtime.upstream_token_cookies()
    if cookies:
        lines.append("# The sandbox's token as the cookie its runtime wants, after the app's own")
        lines.append("# cookies (a browser's own cookie of that name dropped), to that runtime only.")
    for number, name in enumerate(sorted(set(cookies.values())), 1):
        kept, sep = f"{target}_cookie_kept_{number}", f"{target}_cookie_sep_{number}"
        lines.append(f"map ${cookie} ${kept} {{")
        lines.append(f'  "~^(?<before>.*?);\\s*{name}=[^;]*(?<after>.*)$" "$before$after";')
        lines.append(f'  "~^{name}=[^;]*;?\\s*(?<after>.*)$" "$after";')
        lines.append(f"  default ${cookie};")
        lines.append("}")
        lines.append(f'map ${kept} ${sep} {{ "" ""; default "${kept}; "; }}')
    if cookies:
        lines.append(f"map ${source} ${target}_cookie {{")
        lines.append(f"  default ${cookie};")
        numbers = {name: n for n, name in enumerate(sorted(set(cookies.values())), 1)}
        lines += [f'  {kind} "${{{target}_cookie_sep_{numbers[name]}}}{name}=${{{target}_token}}";'
                  for kind, name in cookies.items()]
        lines.append("}")
    return "\n".join(lines) + "\n"


def _https(found: dict) -> bool:
    return any(urlsplit(ingress).scheme == "https" for ingress, _ in found.values())


def upstream_tls(host: str = "sbx_target", indent: str = "    ") -> str:
    """The proxy location's lines for an https ingress: SNI with the host
    the request goes to (the sandbox's: $host, the Host nginx sends), and the
    certificate verified for it against the public CAs; "" while every
    enabled runtime's ingress is plain http (Embed's, on this host)."""
    if not _https(runtime.ingresses()):
        return ""
    return "".join(f"{indent}{line}\n" for line in (
        "proxy_ssl_server_name on;", f"proxy_ssl_name ${host};", "proxy_ssl_verify on;",
        "proxy_ssl_verify_depth 4;", f"proxy_ssl_trusted_certificate {CA_BUNDLE};"))


def _wants_token() -> bool:
    return bool(runtime.upstream_token_headers() or runtime.upstream_token_cookies())


def upstream_token_set(target: str = "sbx_ingress", indent: str = "    ") -> str:
    """The gated location's line that takes the sandbox's token from the gate
    (X-Sbx-Upstream-Token); "" when no enabled runtime wants a token."""
    if not _wants_token():
        return ""
    return f"{indent}auth_request_set ${target}_token $upstream_http_x_sbx_upstream_token;\n"


def upstream_token_send(target: str = "sbx_ingress", indent: str = "    ") -> str:
    """The proxy location's lines that send the token in each declaring
    runtime's header; the map (runtime_upstreams) leaves it empty, so unsent,
    for any other runtime, and a browser's own value of that header never
    passes. A cookie runtime's token goes with the cookies (upstream_cookie)."""
    headers = sorted(set(runtime.upstream_token_headers().values()))
    return "".join(f"{indent}proxy_set_header {header} ${target}_token_{number};\n"
                   for number, header in enumerate(headers, 1))


def upstream_token(target: str = "sbx_ingress", indent: str = "    ") -> str:
    """Both, for a location that takes the token and proxies itself."""
    return upstream_token_set(target, indent) + upstream_token_send(target, indent)


def upstream_cookie(target: str = "sbx_ingress", cookie: str = "sandbox_cookie") -> str:
    """The variable (without $) the proxy sends as Cookie: the app's own
    cookies, with the token appended for a runtime that wants it there."""
    return f"{target}_cookie" if runtime.upstream_token_cookies() else cookie


def cleared_headers() -> list:
    """Every enabled runtime's ingress_cleared: a browser must not pick the VM."""
    return list(dict.fromkeys(name for _, cleared in runtime.ingresses().values() for name in cleared))


def nginx(value, certificate):
    if not value:
        return ''
    ip = ipaddress.ip_address(value)
    if certificate not in ('bootstrap', str(ip)):
        raise ValueError('Invalid IP certificate name')
    cleared = "\n".join(f'    proxy_set_header {name} "";' for name in cleared_headers())
    page_headers = "".join(f"    {line}\n" for line in (
        "proxy_method GET;", "proxy_pass_request_body off;", 'proxy_set_header Content-Length "";',
        f"proxy_set_header Host {host(str(ip))};", "proxy_set_header X-Forwarded-Proto https;",
        f"proxy_set_header X-Original-Host {host(str(ip))};", "proxy_set_header X-Original-Preview-Port $server_port;",
        "proxy_set_header X-Original-URI $request_uri;", "proxy_set_header X-Original-Method $request_method;",
        "proxy_set_header X-Original-Upgrade $http_upgrade;", "proxy_set_header Accept $http_accept;",
        "proxy_set_header Cookie $http_cookie;", "proxy_set_header Authorization $http_authorization;",
        'proxy_set_header Upgrade "";', 'proxy_set_header Connection "";', "proxy_intercept_errors off;"))
    first, last = port_range()
    listeners = '\n'.join(f'  listen {"[::]:" if ip.version == 6 else ""}{p} ssl;' for p in range(first, last + 1))
    return f'''# IP previews share the control plane's renewable IP certificate.
{runtime_upstreams("ip_runtime", "ip_ingress", "ip_cookies")}map $ip_auth $ip_authorization {{ bearer ""; default $http_authorization; }}
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
  # A page for what the gate refused and for what the runtime could not
  # answer (gate.py, preview_pages), and the page's poll.
  location @ip_denied {{
    rewrite ^ /gate/page? break;
    proxy_pass http://127.0.0.1:{platform.CONTROL_PORT};
{page_headers}    proxy_set_header X-Sbx-Reason denied;
  }}
  location @ip_unavailable {{
    rewrite ^ /gate/page? break;
    proxy_pass http://127.0.0.1:{platform.CONTROL_PORT};
{page_headers}    proxy_set_header X-Sbx-Reason unavailable;
  }}
  location = /.sbx/status {{
    rewrite ^ /gate/status? break;
    proxy_pass http://127.0.0.1:{platform.CONTROL_PORT};
{page_headers}    proxy_set_header X-Sbx-Reason "";
  }}
  location / {{
    set $ip_original_method $request_method;
    auth_request /_sbx_ip_gate;
    auth_request_set $ip_upstream $upstream_http_x_sbx_upstream;
    auth_request_set $ip_cookies $upstream_http_x_sbx_cookies;
    auth_request_set $ip_auth $upstream_http_x_sbx_auth;
    auth_request_set $ip_login $upstream_http_x_sbx_login;
    auth_request_set $ip_gone $upstream_http_x_sbx_gone;
    auth_request_set $ip_runtime $upstream_http_x_sbx_runtime;
{upstream_token_set("ip_ingress")}    error_page 401 = @ip_login;
    error_page 403 = @ip_denied;
    try_files /.sbx-no-such-file @ip_sandbox;
  }}
  location @ip_sandbox {{
    proxy_pass $ip_ingress;
{upstream_tls("ip_upstream")}{upstream_token_send("ip_ingress")}    proxy_intercept_errors on;
    error_page 502 504 = @ip_unavailable;
    proxy_connect_timeout 15s;
    proxy_http_version 1.1;
    proxy_set_header Host $ip_upstream;
{cleared}
    proxy_set_header X-Forwarded-Host {host(str(ip))}:$server_port;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header Cookie ${upstream_cookie("ip_ingress", "ip_cookies")};
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
    elif sys.argv[1:] == ['upstreams']:
        print(runtime_upstreams(), end='')
    elif sys.argv[1:] == ['upstream-tls']:
        print(upstream_tls(), end='')
    elif sys.argv[1:] == ['upstream-token']:
        print(upstream_token(), end='')
    elif sys.argv[1:] == ['upstream-token-set']:
        print(upstream_token_set(), end='')
    elif sys.argv[1:] == ['upstream-token-send']:
        print(upstream_token_send(), end='')
    elif sys.argv[1:] == ['upstream-cookie']:
        print(upstream_cookie(), end='')
    elif sys.argv[1:] == ['default-ingress']:
        print(default_ingress())
    else:
        raise SystemExit('usage: python -m sbx_control.ip_previews nginx ADDRESS CERTIFICATE | upstreams | '
                         'upstream-tls | upstream-token[-set|-send] | upstream-cookie | default-ingress')
