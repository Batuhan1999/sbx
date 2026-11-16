"""Make Embed enforce what sandboxes may reach (hosted_services.allowed_cidrs):
its slot firewall, which recreating the runtime applies before sandboxes
are created, and this host's forwarding rules for its slots' veths.

guard() is Embed's part of sbx-host-guard (host-guard.sh), after the
platform's rules. Sandboxes' TCP leaves through Embed's egress proxy in the
orchestrator's container, i.e. from this host itself, so per-sandbox rules
and the IMDS hop limit don't apply to it. For that container's cgroup only:
no cloud metadata service, and none of this host's own addresses but
loopback (the orchestrator's own) and network.host_ports (443: nginx and
the gate). On bare metal the public address is on an interface, and the
proxy would otherwise reach every port the host listens on. It runs at boot
and whenever install.sh recreates Embed's containers (the orchestrator's
cgroup changes with its container)."""
import fcntl
import ipaddress
import shlex
import subprocess

from ... import core, hosted_services, paths, platform
from . import host


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
    if not unchanged and core.sandboxes_complete():
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
    return {'status': 'unchanged' if unchanged else 'applied',
            'allowed_cidrs': hosted_services.allowed_cidrs()}


def _compose_up():
    subprocess.run(['docker', 'compose', 'up', '-d', '--wait'], cwd=host.EMBED, check=True, timeout=600,
                   stdout=subprocess.DEVNULL)


def guard():
    egress_rules()
    apply_routes()


def egress_rules():
    """SBX-EGRESS for the orchestrator's cgroup, replacing an earlier run's."""
    from ... import host as platform_host
    # Docker Compose names the container <project>-<service>-1; the project is /opt/e2b.
    container = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", "e2b-orchestrator-1"], check=True,
                               capture_output=True, text=True).stdout.strip()
    cgroup = f"system.slice/docker-{container}.scope"
    host_ports = platform_host.shell()["host_ports"].split()
    for tool, loopback in (("iptables", "127.0.0.0/8"), ("ip6tables", "::1/128")):
        def rule(*args, check=True):
            return subprocess.run([tool, "-w", *args], check=check, capture_output=True, text=True)
        for chain in ("OUTPUT", "INPUT"):
            for line in rule("-S", chain).stdout.splitlines():
                if line.startswith("-A ") and "-j SBX-EGRESS" in line:
                    rule("-D", *shlex.split(line)[1:])
        if rule("-N", "SBX-EGRESS", check=False).returncode:
            rule("-F", "SBX-EGRESS")
        if tool == "iptables":
            rule("-A", "SBX-EGRESS", "-d", "169.254.169.254/32", "-j", "REJECT")
        for port in host_ports:
            rule("-A", "SBX-EGRESS", "-p", "tcp", "--dport", port, "-m", "addrtype", "--dst-type", "LOCAL",
                 "-j", "RETURN")
        rule("-A", "SBX-EGRESS", "-p", "tcp", "-m", "addrtype", "--dst-type", "LOCAL", "!", "-d", loopback,
             "-j", "REJECT", "--reject-with", "tcp-reset")
        rule("-I", "OUTPUT", "-m", "cgroup", "--path", cgroup, "-j", "SBX-EGRESS")
