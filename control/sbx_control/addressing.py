"""Where things are reached: on the domain's host names
(<slug><suffix>.<domain>, <port>-<host id>.<domain>), or, on an
installation with only an IP, on the HTTPS ports ip_previews assigns.

Every function decides from the settings as they are now; nothing is kept
from the start."""
import re

from . import ip_previews, recipes, runtime, settings

# <port>-<host id>.<domain>: a sandbox's port by its runtime's sandbox id,
# spelled as a host name label (host_id).
RAW_HOST = re.compile(r"^(\d+)-([a-z0-9-]+)$")   # the id's spelling: sandbox_id_of decides
_ESCAPE = re.compile(r"-([0-9a-f]{2})")


def host_id(sandbox_id: str) -> str:
    """The runtime's sandbox id as it goes in a host name: lowercase letters
    and digits as they are, every other byte (an underscore, a hyphen, a
    capital) as -<two hex digits>, since a DNS label holds only [a-z0-9-]
    and hosts are matched case-blind. Ids of letters and digits keep their
    host; a Boat id bx_3f8tnb7k becomes bx-5f3f8tnb7k."""
    return "".join(c if c in "abcdefghijklmnopqrstuvwxyz0123456789" else
                   "".join(f"-{b:02x}" for b in c.encode()) for c in sandbox_id)


def sandbox_id_of(label: str) -> str | None:
    """host_id's inverse; None for what host_id never makes."""
    if not re.fullmatch(r"(?:[a-z0-9]|-[0-9a-f]{2})+", label):
        return None
    try:
        decoded = _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), label).encode("latin-1").decode()
    except UnicodeError:
        return None
    return decoded if host_id(decoded) == label else None   # one host per sandbox: no escape of a-z0-9


def domain() -> str:
    """The installation's domain, or "" on an IP-only installation."""
    return settings.current().domain.name


def host_suffix() -> str:
    current = domain()
    return f".{current}" if current else ""


def mode() -> str:
    return "domain" if domain() else "ip"


def previews(a: recipes.App) -> dict:
    """Every port a sandbox of the app exposes, by host suffix: <slug>.<domain>
    for the main preview, <slug>-<name> for the rest; the recipe's, then the
    installation's (settings), which win on a clash. The gate lets nothing
    else through."""
    return {**a.previews(), **recipes.parse_previews(settings.current().sandboxes.previews)}


def ports(a: recipes.App) -> dict:
    return {suffix: p["port"] for suffix, p in previews(a).items()}


def preview_on(a: recipes.App, port: int):
    return next((p for p in previews(a).values() if p["port"] == port), None)


def urls(slug: str, a) -> dict:
    """The app on the sandbox's readable hostnames, one origin for every
    browser, the desktop's Chromium included. "web" is the main preview."""
    current = domain()
    if not current:
        found = {}
        for suffix, preview in previews(a).items():
            if not preview["link"]:
                continue
            try:
                url = ip_previews.origin(slug, preview["port"])
            except ip_previews.PortPoolExhausted:
                continue
            if url:
                found[suffix.lstrip("-") or "web"] = url + preview["path"]
        return found
    return {suffix.lstrip("-") or "web": f"https://{slug}{suffix}.{current}{p['path']}"
            for suffix, p in previews(a).items() if p["link"]}


def env_urls(slug: str, a) -> dict:
    """SBX_URL for the main preview, SBX_URL_<PREVIEW> for the rest. Unlike
    urls(), a full IP port pool is an error: the sandbox would get no URL."""
    current = domain()
    env = {}
    exposed = previews(a)
    for suffix in (s for s, p in exposed.items() if p["link"]):
        key = "SBX_URL" + re.sub(r"[^A-Z0-9]", "_", suffix.upper())
        if current:
            env[key] = f"https://{slug}{suffix}.{current}"
        elif url := ip_previews.origin(slug, exposed[suffix]["port"]):
            env[key] = url
    return env


def ssh_url(slug: str, a, sandbox_id: str) -> str:
    """Where the CLI's ProxyCommand reaches the sandbox's SSH-over-WebSocket
    bridge: <port>-<host id>.<domain>, which names the VM itself (a new VM
    under the same name is another host). An IP-only installation has no
    such hostnames, so the bridge gets an assigned port like any other
    preview."""
    port = ports(a).get("-ssh")
    if not port:
        return ""
    current = domain()
    if current:
        return f"wss://{port}-{host_id(sandbox_id)}.{current}"
    try:
        origin = ip_previews.origin(slug, port)
    except ip_previews.PortPoolExhausted:
        return ""
    return origin.replace("https://", "wss://", 1)


def detected_origin(slug: str, sandbox_id: str, port: int, *, raw: bool) -> str:
    """The link of a detected preview: <slug>-<port>, or the raw
    <port>-<host id> host when raw; an assigned port without a domain (""
    when none can be given). Raises ip_previews.PortPoolExhausted when the pool is full."""
    current = domain()
    if not current:
        return ip_previews.origin(slug, port)
    host = f"{port}-{host_id(sandbox_id)}" if raw else f"{slug}-{port}"
    return f"https://{host}.{current}"


def detected_pattern(slug: str) -> str:
    """How a detected preview's host looks, for an agent's instructions."""
    return f"https://{slug}-<port>.{domain()}"


def preview_pattern(suffix: str, path: str = "") -> str:
    """How a repo's declared preview is reached, for its page."""
    current = domain()
    return (f"<name>{suffix}.{current}" if current else f"{settings.current().base_url}:<assigned-port>") + path


def vite_allowed_hosts(kind: str | None = None) -> str:
    """Vite rejects unknown Host headers; a leading dot allows subdomains.
    Without a domain, the internal host of the sandbox's runtime (kind)."""
    return host_suffix() or f".{runtime.internal_suffix(kind)}"


def control_origin(request_origin: str) -> str:
    """The origin the web app is served from: the control host with a
    domain; without one, whatever address the request came to."""
    return settings.current().base_url if domain() else request_origin


def on_our_hosts(url) -> bool:
    """Whether a parsed https URL names a host under the domain as a browser
    reads it: a plain host (and port), without user info, a backslash (which
    a browser takes for a slash, so https://evil.example\\.<domain> is
    evil.example) or anything else a host name does not hold."""
    current = domain()
    return (bool(current) and url.scheme == "https" and (url.hostname or "").endswith(f".{current}")
            and re.fullmatch(r"[a-z0-9.-]+(?::\d+)?", url.netloc.lower()) is not None)


def service_hosts() -> set:
    """The operator's proxied services (settings proxied.services) by host name."""
    current = domain()
    return {f"{name}.{current}" for name in settings.current().proxied.services}


def host_label(host: str) -> str:
    return host.removesuffix(host_suffix() or ".")


def raw_host(host: str):
    """<port>-<host id>.<domain> -> (port, sandbox id); None for any other host."""
    current = domain()
    if not current or not host.endswith(f".{current}"):
        return None
    match = RAW_HOST.match(host[:-len(current) - 1])
    sandbox_id = sandbox_id_of(match.group(2)) if match else None
    return (int(match.group(1)), sandbox_id) if sandbox_id else None


def upstream(port: int, sandbox_id: str) -> str:
    """The Host nginx sends a sandbox port's requests to the runtime with."""
    return runtime.upstream_host(port, sandbox_id)
