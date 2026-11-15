"""Make Embed enforce what sandboxes may reach (hosted_services.allowed_cidrs):
its slot firewall, which recreating the runtime applies before sandboxes
are created, and this host's forwarding rules for its slots' veths."""
import fcntl
import ipaddress
import subprocess

from ... import core, host, hosted_services, paths, platform, settings


def apply_routes():
    """Docker defaults to dropping forwarded traffic from Embed's slot veths."""
    for tool, version in (("iptables", 4), ("ip6tables", 6)):
        def rule(*args, check=True):
            return subprocess.run([tool, "-w", *args], check=check, capture_output=True)
        rule("-N", "SBX-SERVICES", check=False)
        rule("-F", "SBX-SERVICES")
        rule("-t", "raw", "-N", "SBX-SERVICES-RAW", check=False)
        rule("-t", "raw", "-F", "SBX-SERVICES-RAW")
        for cidr in hosted_services.allowed_cidrs():
            if ipaddress.ip_network(cidr).version != version:
                continue
            rule("-A", "SBX-SERVICES", "-i", "veth-+", "-d", cidr, "-p", "tcp", "-j", "ACCEPT")
            # Docker's direct-routing guard drops container IP traffic before FORWARD.
            rule("-t", "raw", "-A", "SBX-SERVICES-RAW", "-i", "veth-+", "-d", cidr,
                 "-p", "tcp", "-j", "ACCEPT")
            rule("-A", "SBX-SERVICES", "-s", cidr, "-o", "veth-+", "-m", "conntrack",
                 "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
        if rule("-C", "FORWARD", "-j", "SBX-SERVICES", check=False).returncode:
            rule("-I", "FORWARD", "1", "-j", "SBX-SERVICES")
        if rule("-t", "raw", "-C", "PREROUTING", "-j", "SBX-SERVICES-RAW", check=False).returncode:
            rule("-t", "raw", "-I", "PREROUTING", "1", "-j", "SBX-SERVICES-RAW")


def apply():
    platform.connect()
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    with (paths.state_dir() / 'network-apply.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _apply()


def _apply():
    env = host.EMBED / ".env"
    before = env.read_text()
    unchanged = host.render_embed_env(before, host.embed_env()) == before
    # The installed Embed revision has not passed restart/resume acceptance.
    # A pause is not a proven maintenance barrier, so never restart under a sandbox.
    if not unchanged and core.sandboxes():
        raise ValueError('apply this before creating sandboxes: the runtime is recreated, which existing sandboxes '
                         'do not survive')
    if not unchanged:
        host.write_embed_env()
        try:
            _compose_up()
        except Exception:
            env.write_text(before)
            _compose_up()
            raise
        finally:
            subprocess.run(['systemctl', 'restart', 'sbx-host-guard'], check=True, timeout=30)
    apply_routes()
    (paths.run() / 'apply' / settings.HostUnit.EMBED_NETWORK.value).unlink(missing_ok=True)
    return {'status': 'unchanged' if unchanged else 'applied',
            'allowed_cidrs': hosted_services.allowed_cidrs()}


def _compose_up():
    subprocess.run(['docker', 'compose', 'up', '-d', '--wait'], cwd=host.EMBED, check=True, timeout=600,
                   stdout=subprocess.DEVNULL)


if __name__ == '__main__':   # sbx-host-guard, after its own rules
    apply_routes()
