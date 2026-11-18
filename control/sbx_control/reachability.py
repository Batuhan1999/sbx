"""Can sandboxes reach this server at its public address?

Sandboxes call back to sbx-control at settings.base_url: a template build
downloads its repo bundle there, git asks it for credentials, catalog
actions run through it. When that address does not work from where
sandboxes run (DNS, a firewall, NAT, a made-up domain), all of them fail
late and say little. This check finds out at setup and in sbx-admin
doctor: a throwaway sandbox from the base template fetches the public,
unauthenticated /dist/version over base_url, and is killed whatever
happens. It only goes through sbx_control.runtime."""
from __future__ import annotations

import shlex
import sys
from urllib.parse import urlsplit

from . import runtime, settings
from .connections.base import Check

NAME = "Sandboxes reach this server"
PATH = "/dist/version"
TIMEOUT = 120         # seconds the sandbox lives at most, if killing it fails
CURL_SECONDS = 20
BODY = "/tmp/sbx-reachable"

# curl's exit codes (man curl, EXIT CODES) by what they mean here.
DNS = {6}
TCP = {7, 28}
TLS = {35, 51, 53, 54, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91}


def probe(url: str, pin: str) -> str:
    """The command run in the sandbox: always exits 0; prints curl's exit
    code and the HTTP status on its first line, then what came back; curl's
    own error goes to stderr."""
    curl = (f"curl -sS -o {BODY} -w '%{{http_code}}' --connect-timeout 10 --max-time {CURL_SECONDS} "
            f"{pin + ' ' if pin else ''}{shlex.quote(url)}")
    script = f'code=$({curl}); rc=$?; echo "$rc $code"; head -c 200 {BODY} 2>/dev/null; true'
    return f"bash -c {shlex.quote(script)}"


def explain(url: str, rc: int, status: str, body: str, error: str, expected: str) -> Check:
    """The check from what curl said inside the sandbox."""
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    said = f": {error.strip()}" if error.strip() else ""
    if rc in DNS:
        return Check(NAME, False, f"{host} does not resolve from inside a sandbox{said}",
                     f"publish a DNS record for {host} that points at this server, "
                     "or give this server an address sandboxes can resolve")
    if rc in TCP:
        what = "timed out" if rc == 28 else "was refused or is unreachable"
        return Check(NAME, False, f"a connection from a sandbox to {host}:{port} {what}{said}",
                     f"let sandboxes reach this server's public address on port {port}: the host firewall, "
                     "the cloud's security group, or NAT that does not route back to this server (hairpin)")
    if rc == 90:
        return Check(NAME, False, f"{host} answered a sandbox with a certificate that is not this server's{said}",
                     f"something else answers for {host} from where sandboxes run; point it at this server")
    if rc in TLS:
        return Check(NAME, False, f"TLS to {host} failed from inside a sandbox{said}",
                     "the certificate must be valid for this name (see the domain check), or self-signed "
                     "by this server")
    if rc != 0:
        return Check(NAME, False, f"curl from a sandbox to {url} failed (exit {rc}){said}",
                     "run the same curl in a sandbox to see more")
    if status != "200":
        return Check(NAME, False, f"{url} answered a sandbox with HTTP {status}",
                     "something other than sbx-control answers at this address from where sandboxes run: "
                     "check the proxy or load balancer in front of it")
    if expected and body.strip() != expected:
        return Check(NAME, False, f"{url} answered a sandbox, but not as this server (release "
                     f"{body.strip()[:40]!r}, this is {expected!r})",
                     "the address leads to another sbx installation from where sandboxes run")
    return Check(NAME, True, f"a sandbox fetched {url}")


def check(kind: str | None = None) -> Check:
    """Start a sandbox from the base template on the runtime of kind (None:
    the default), fetch PATH over base_url from inside it, and kill it,
    whatever happened. The check names the runtime (Check.runtime)."""
    kind = kind or runtime.default()
    found = _check(kind)
    return Check(found.name, found.ok, found.detail, found.fix, runtime=kind)


def _check(kind: str) -> Check:
    from . import dist_api, template_builds, templates
    base = settings.current().base_url
    if not base:
        return Check(NAME, False, "this server has no public address yet", "choose a domain or an IP address")
    try:
        template = template_builds.built(templates.BASE, kind)
    except template_builds.NotBuilt as not_built:
        build = template_builds.state(templates.BASE, kind)
        if build.get("status") == "failed":
            error = (build.get("error") or "").strip()[-300:] or "no error recorded"
            return Check(NAME, False, f"no sandbox to check from: the base template's build failed: {error}",
                         f"fix the cause, then `sbx-admin template rm {templates.BASE}`; "
                         "the scheduler builds it again within minutes")
        if build.get("status") in ("building", "interrupted"):
            return Check(NAME, True, "not checked yet: the base template is still building; "
                         "sbx-admin doctor checks it once it is built")
        return Check(NAME, False, f"no sandbox to check from: {not_built}",
                     "run sbx-admin doctor again once the base template is built")
    url = base + PATH
    try:
        sbx = runtime.create(template, timeout=TIMEOUT, metadata={"sbx_check": "reachability"}, on_timeout="kill",
                             kind=kind)
    except Exception as error:
        return Check(NAME, False, f"no sandbox to check from: {error}", "see the runtime's own checks")
    try:
        result = sbx.commands.run(probe(url, templates.pinned_key()), timeout=CURL_SECONDS + 30)
        first, _, body = (result.stdout or "").partition("\n")
        rc, _, status = first.strip().partition(" ")
        return explain(url, int(rc) if rc.lstrip("-").isdigit() else -1, status.strip(), body,
                       result.stderr or "", dist_api.dist_version())
    except Exception as error:
        return Check(NAME, False, f"the check could not run in its sandbox: {error}", "see the runtime's own checks")
    finally:
        try:
            sbx.kill()
        except Exception as error:  # it times out on its own (on_timeout="kill")
            print(f"reachability: killing {sbx.sandbox_id}: {error!r}", file=sys.stderr)
