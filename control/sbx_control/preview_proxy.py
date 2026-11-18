"""nginx's side of previews, for domain hosts (tls.sh) and IP previews
(ip_previews.nginx) alike: one upstream per enabled sandbox runtime and the
map from the kind the gate names to it, TLS to an https ingress, a runtime's
token in its header, the Cookie header the gate writes (upstream_cookie) and
the locations of sbx's preview page and its poll.

  python -m sbx_control.preview_proxy upstreams | upstream-tls | upstream-token-set |
                                      upstream-token-send | page-locations LOGIN_HOST | default-ingress
"""
import sys
from urllib.parse import urlsplit

from . import platform, runtime
from .runtime.base import TokenPlace

# The CA bundle in nginx's image (alpine's ca-certificates): what an https
# ingress's certificate is verified against.
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
NO_INGRESS = "http://127.0.0.1:9"   # the discard port: nothing listens


def _named(place: str) -> list[str]:
    return sorted({p.name for p in runtime.upstream_token_places().values() if p.place == place})


# The Cookie header an upstream gets


def app_cookies(value: str, *without: str) -> str:
    """The browser's cookies without sbx's session (it must reach only the
    gate; cookies are host-scoped, ports alike) or any named in without,
    even when a client sends a name twice."""
    drop = {platform.SESSION_COOKIE, *without}
    return '; '.join(part.strip() for part in value.split(';')
                     if part.partition('=')[0].strip() not in drop and part.strip())


def upstream_cookie(cookies: str, token_cookie: str | None = None, token: str = "") -> str:
    """The whole Cookie header the gate has nginx send upstream: the app's
    own cookies and, for a runtime that wants its token in a cookie
    (token_cookie), that cookie with the token instead of any the browser sent."""
    if not token_cookie or not token:
        return app_cookies(cookies)
    return "; ".join(part for part in (app_cookies(cookies, token_cookie), f"{token_cookie}={token}") if part)


# Upstreams


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


def runtime_upstreams(source: str = "sbx_runtime", target: str = "sbx_ingress") -> str:
    """nginx: one upstream block per enabled runtime (its ingress) and a map
    from the kind the gate names (X-Sbx-Runtime, in $source) to it ($target);
    anything else goes to the default runtime's. For a runtime that wants
    its sandboxes' token in a header, a map that sends it to that runtime
    only (a cookie runtime's token is in the Cookie header the gate writes)."""
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
    places = runtime.upstream_token_places()
    headers = _named("header")
    if headers:
        lines.append("# The sandbox's token, in the header its runtime wants, to that runtime only.")
    for number, header in enumerate(headers, 1):
        lines.append(f"map ${source} ${target}_token_{number} {{")
        lines.append('  default "";')
        lines += [f"  {kind} ${target}_token;" for kind, p in places.items() if p == TokenPlace("header", header)]
        lines.append("}")
    return "\n".join(lines) + "\n"


def upstream_tls(host: str = "sbx_target", indent: str = "    ") -> str:
    """The proxy location's lines for an https ingress: SNI with the host
    the request goes to (the sandbox's: $host, the Host nginx sends), and the
    certificate verified for it against the public CAs; "" while every
    enabled runtime's ingress is plain http (Embed's, on this host)."""
    if not any(urlsplit(ingress).scheme == "https" for ingress, _ in runtime.ingresses().values()):
        return ""
    return "".join(f"{indent}{line}\n" for line in (
        "proxy_ssl_server_name on;", f"proxy_ssl_name ${host};", "proxy_ssl_verify on;",
        "proxy_ssl_verify_depth 4;", f"proxy_ssl_trusted_certificate {CA_BUNDLE};"))


def upstream_token_set(target: str = "sbx_ingress", indent: str = "    ") -> str:
    """The gated location's line that takes the sandbox's token from the gate
    (X-Sbx-Upstream-Token) for a header runtime; "" when no enabled runtime
    wants one in a header."""
    if not _named("header"):
        return ""
    return f"{indent}auth_request_set ${target}_token $upstream_http_x_sbx_upstream_token;\n"


def upstream_token_send(target: str = "sbx_ingress", indent: str = "    ") -> str:
    """The proxy location's lines that send the token in each declaring
    runtime's header; the map (runtime_upstreams) leaves it empty, so unsent,
    for any other runtime, and a browser's own value of that header never
    passes."""
    lines = [f"proxy_set_header {header} ${target}_token_{number};"
             for number, header in enumerate(_named("header"), 1)]
    # nginx can't drop one Set-Cookie; a token cookie the provider sets is at least kept from scripts.
    lines += [f"proxy_cookie_flags {name} httponly secure samesite=strict;" for name in _named("cookie")]
    return "".join(f"{indent}{line}\n" for line in lines)


def upstream_token(target: str = "sbx_ingress", indent: str = "    ") -> str:
    """Both, for a location that takes the token and proxies itself."""
    return upstream_token_set(target, indent) + upstream_token_send(target, indent)


def cleared_headers() -> list:
    """Every enabled runtime's ingress_cleared: a browser must not pick the VM."""
    return list(dict.fromkeys(name for _, cleared in runtime.ingresses().values() for name in cleared))


# sbx's preview page and its poll


def page_locations(*, host: str, original_host: str, preview_port: str, sandbox_headers: bool,
                   prefix: str = "", indent: str = "") -> str:
    """The locations that send a refused request (@<prefix>denied), what the
    runtime could not answer (@<prefix>unavailable) and the page's poll
    (/.sbx/status) to sbx-control's preview page (gate.py, preview_pages).
    host: the Host sbx-control is asked with; original_host and preview_port
    what the gate reads the preview from; sandbox_headers: pass a runtime's
    own routing headers (Embed's e2b-sandbox-id and -port), else none."""
    sandbox = ("$http_e2b_sandbox_id", "$http_e2b_sandbox_port") if sandbox_headers else ('""', '""')
    headers = (
        "proxy_method GET;", "proxy_pass_request_body off;", 'proxy_set_header Content-Length "";',
        f"proxy_set_header Host {host};", "proxy_set_header X-Forwarded-Proto https;",
        f"proxy_set_header X-Original-Host {original_host};", "proxy_set_header X-Original-URI $request_uri;",
        "proxy_set_header X-Original-Method $request_method;", "proxy_set_header X-Original-Upgrade $http_upgrade;",
        f"proxy_set_header X-Original-Sandbox-Id {sandbox[0]};",
        f"proxy_set_header X-Original-Sandbox-Port {sandbox[1]};",
        f"proxy_set_header X-Original-Preview-Port {preview_port};", "proxy_set_header Accept $http_accept;",
        "proxy_set_header Cookie $http_cookie;", "proxy_set_header Authorization $http_authorization;",
        'proxy_set_header Upgrade "";', 'proxy_set_header Connection "";', "proxy_intercept_errors off;")
    blocks = []
    for location, path, reason in ((f"@{prefix}denied", "/gate/page", "denied"),
                                   (f"@{prefix}unavailable", "/gate/page", "unavailable"),
                                   ("= /.sbx/status", "/gate/status", '""')):
        body = [f"rewrite ^ {path}? break;", f"proxy_pass http://127.0.0.1:{platform.CONTROL_PORT};", *headers,
                f"proxy_set_header X-Sbx-Reason {reason};"]
        blocks.append(f"{indent}location {location} {{\n" + "".join(f"{indent}  {line}\n" for line in body)
                      + f"{indent}}}\n")
    return "".join(blocks)


def main(argv: list) -> int:
    if argv == ["upstreams"]:
        print(runtime_upstreams(), end="")
    elif argv == ["upstream-tls"]:
        print(upstream_tls(), end="")
    elif argv == ["upstream-token-set"]:
        print(upstream_token_set(), end="")
    elif argv == ["upstream-token-send"]:
        print(upstream_token_send(), end="")
    elif argv[:1] == ["page-locations"] and len(argv) == 2:
        print(page_locations(host=argv[1], original_host="$host", preview_port='""', sandbox_headers=True), end="")
    elif argv == ["default-ingress"]:
        print(default_ingress())
    else:
        raise SystemExit(__doc__.rsplit("\n\n", 1)[1].strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
