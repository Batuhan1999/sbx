"""Which sandbox and port a preview host reaches: the slugs of the running
installation, and detected previews.

Detected previews are what servers listen on in running sandboxes, looked at
by the keeper every minute and by the gate when a request names a port it
doesn't know yet, so a server started a moment ago works within seconds."""
import time

from . import addressing, core, ip_previews, recipes, runtime, settings

DETECT_AGAIN_SECONDS = 10

_slugs: dict = {"fetched_at": 0.0, "by_slug": {}}
_detected: dict = {}  # sandbox id -> {port: listens on loopback only}
_detected_at: dict = {}  # sandbox id -> when it was last looked at


def slugs(fresh=False) -> dict:
    """slug -> (sandbox id, app), at most 20 s old."""
    if fresh or time.time() - _slugs["fetched_at"] > 20:
        _slugs["by_slug"] = {core.slug_of(i): (i.sandbox_id, core.app_of(i)) for i in core.sandboxes() if core.slug_of(i)}
        _slugs["fetched_at"] = time.time()
    return _slugs["by_slug"]


def readable_host(host: str):
    """<slug><suffix>.<domain> -> (port, sandbox id), with the previews of
    that sandbox's app, then <slug>-<port> for a detected one (the gate
    checks that something listens there); None when it names nothing. The
    exact slug wins over a suffix, so a slug may itself end in -api or -8080."""
    label = addressing.host_label(host)
    by_slug = slugs()
    cuts = [len(label)] + [i for i in range(len(label) - 1, 0, -1) if label[i] == "-"]
    for cut in cuts:
        slug, suffix = label[:cut], label[cut:]
        if slug in by_slug:
            sandbox_id, a = by_slug[slug]
            if suffix in addressing.ports(a):
                return addressing.ports(a)[suffix], sandbox_id
            port = core.suffix_port(suffix)
            if settings.current().sandboxes.detected_previews != "off" and port and core.detectable(port, a):
                return port, sandbox_id
    return None


def port_host(slug: str) -> bool:
    """Whether the slug is another sandbox's slug plus -<port>: it would take
    over that sandbox's detected preview host, as the exact slug wins."""
    base, _, port = slug.rpartition("-")
    return settings.current().sandboxes.detected_previews != "off" and bool(core.suffix_port(f"-{port}")) and base in slugs(fresh=True)


def known_ports(sandbox_id: str) -> dict:
    return _detected.get(sandbox_id, {})


def detection_due(sandbox_id: str) -> bool:
    return time.time() - _detected_at.get(sandbox_id, 0) > DETECT_AGAIN_SECONDS


def detect_ports(sandbox_id: str, a: recipes.App) -> dict:
    """What listens in the sandbox now; it must be running, as connecting
    resumes a paused one. On failure, what was known before."""
    _detected_at[sandbox_id] = time.time()
    try:
        listening = runtime.connect(sandbox_id).commands.run(core.LISTENING, timeout=10).stdout
        _detected[sandbox_id] = core.detected_ports(listening, a)
        if addressing.mode() == "ip":
            ip_previews.observe(sandbox_id, addressing.ports(a).values(), _detected[sandbox_id])
    except runtime.NotRunning:
        pass  # it paused or went meanwhile
    except Exception as error:
        print(f"detecting ports in {sandbox_id}: {error!r}", flush=True)
    return known_ports(sandbox_id)


def forget_except(sandbox_ids):
    for gone in _detected.keys() - set(sandbox_ids):
        _detected.pop(gone, None), _detected_at.pop(gone, None)


def detected_preview(sandbox_id: str, port: int, a: recipes.App, running: bool):
    """The preview a server on this port makes, or None. An unknown port
    makes the gate look again, at most every DETECT_AGAIN_SECONDS per
    sandbox, and only in a running sandbox: a request for a port nothing
    listened on before doesn't resume one."""
    if settings.current().sandboxes.detected_previews == "off" or not core.detectable(port, a):
        return None
    ports = known_ports(sandbox_id)
    if port not in ports and running and detection_due(sandbox_id):
        ports = detect_ports(sandbox_id, a)
    if ports.get(port) is not False:  # not listening, or on loopback only
        return None
    return {"port": port, "access": settings.current().sandboxes.detected_previews}


def detected_urls(info) -> tuple[dict, list]:
    """(port-<n> -> link of each detected preview, every detected port with
    its link or loopback: true). The link is <slug>-<port> unless the app
    declares that suffix or another sandbox has it as its slug; then the raw
    <port>-<host id> host."""
    if settings.current().sandboxes.detected_previews == "off":
        return {}, []
    slug, a = core.slug_of(info), core.app_of(info)
    links, ports = {}, []
    for port, loopback in sorted(known_ports(info.sandbox_id).items()):
        if loopback:
            ports.append({"port": port, "loopback": True})
            continue
        raw = addressing.mode() == "domain" and (f"-{port}" in addressing.ports(a) or f"{slug}-{port}" in slugs())
        try:
            url = addressing.detected_origin(slug, info.sandbox_id, port, raw=raw)
        except ip_previews.PortPoolExhausted:
            ports.append({"port": port, "preview_error": "IP preview port pool is full"})
            continue
        if url:
            links[f"port-{port}"] = url
        ports.append({"port": port, "url": url, "access": settings.current().sandboxes.detected_previews})
    return links, ports
