"""Can a sandbox reach the host it runs on? Run on the host, as root:

  python -m sbx_control.runtime.embed.netcheck [--template NAME]   (default: the base template built last)

Starts a throwaway sandbox and, from inside it, tries every TCP port the
host listens on, at every address the host has. Only the settings'
network.host_ports (443: nginx and the gate) may answer: anything else, such as
sbx-control on 8080 or Embed's orchestrator on 5008, would let an agent around
the gate. It also checks, on the host, that install.sh's sbx-host-guard keeps
Embed's egress proxy off the cloud metadata service. Prints what answered and exits 1
if anything it should not have did.

Embed redirects all of a sandbox's TCP to its egress proxy, which accepts
every connection before it dials the real destination, so a connect alone
says nothing: a target counts as reached only when something answers a
request.
"""
import argparse
import ipaddress
import json
import socket
import subprocess
import sys

from ... import platform, runtime, settings

METADATA = "169.254.169.254"


def host_addresses() -> list:
    """The host's IPv4 addresses a sandbox could aim at: loopback aside, and
    not the host ends of other sandboxes' slots (Embed's veth-N, one per
    slot); the sandbox's own gateway is added from inside it."""
    out = json.loads(subprocess.run(["ip", "-j", "-4", "addr"], check=True, capture_output=True, text=True).stdout)
    return sorted({a["local"] for i in out if not i["ifname"].startswith(("veth", "tap"))
                   for a in i.get("addr_info", []) if not ipaddress.ip_address(a["local"]).is_loopback})


def listening_ports() -> list:
    out = subprocess.run(["ss", "-Hltn"], check=True, capture_output=True, text=True).stdout
    return sorted({int(line.split()[3].rpartition(":")[2]) for line in out.splitlines() if line.strip()})


PROBE = r"""#!/bin/bash
# probe ADDRESS PORT: prints ADDRESS:PORT when anything answers there.
exec 3<>"/dev/tcp/$1/$2" || exit 1
printf 'HEAD / HTTP/1.0\r\nHost: %s\r\n\r\n' "$1" >&3
[ "$(head -c1 <&3 | wc -c)" = 1 ] && echo "$1:$2"
"""


def probe(sbx, targets: list) -> list:
    """(address, port) pairs that answer from inside the sandbox, all tried at
    once: a minimal HTTP request (a server that speaks first, like ssh,
    answers anyway), and reached when any byte comes back."""
    sbx.files.write("/tmp/sbx-probe", PROBE)
    script = "".join(f"timeout 3 bash /tmp/sbx-probe {a} {p} 2>/dev/null & " for a, p in targets)
    reached = sbx.commands.run(f"bash -c {json.dumps(script + 'wait')}", timeout=60).stdout.split()
    return [(a, int(p)) for a, _, p in (r.rpartition(":") for r in reached)]


def metadata_guarded() -> bool:
    """install.sh's sbx-host-guard in place for the running orchestrator
    container (its egress proxy dials for sandboxes from the host): the jump
    for its cgroup, and the metadata service rejected there."""
    cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", "e2b-orchestrator-1"],
                         capture_output=True, text=True).stdout.strip()
    rules = lambda chain: subprocess.run(["iptables", "-w", "-S", chain], capture_output=True, text=True).stdout
    jump = any(f"docker-{cid}.scope" in r and "-j SBX-EGRESS" in r for r in rules("OUTPUT").splitlines())
    return bool(cid) and jump and any(METADATA in r and "REJECT" in r for r in rules("SBX-EGRESS").splitlines())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template")
    args = parser.parse_args()
    platform.connect()
    if not args.template:
        from ... import template_builds, templates
        args.template = template_builds.built(templates.BASE)
    # On a cloud VM the public address is not on an interface; the domain names it.
    addresses = set(host_addresses()) | {socket.gethostbyname(f"{settings.current().domain.login_host}.{settings.current().domain.name}")}
    ports = listening_ports()
    sbx = runtime.create(args.template, timeout=600, metadata={"sbx": "netcheck"})
    try:
        gateway = sbx.commands.run("ip route | awk '/^default/ {print $3}'").stdout.strip()
        if gateway:
            addresses.add(gateway)
        addresses = sorted(addresses)
        targets = [(a, p) for a in addresses for p in ports]
        print(f"host addresses {', '.join(addresses)}; listening on {', '.join(map(str, ports))}", flush=True)
        # Inside the VM; Embed may translate it before the host sees it. While
        # this runs, `conntrack -L | grep 8080` on the host shows the source.
        print(f"sandbox's own address: {sbx.commands.run('hostname -I').stdout.strip()}")
        reached = probe(sbx, targets)
        internet = sbx.commands.run("curl -s -m10 -o /dev/null -w '%{http_code}' https://github.com || true").stdout
    finally:
        sbx.kill()
    allowed = set(settings.current().network.host_ports)
    bad = [(a, p) for a, p in reached if p not in allowed]
    for a, p in reached:
        print(f"{'ALLOWED' if (a, p) not in bad else 'OPEN   '} {a}:{p}")
    if not internet.startswith(("2", "3")):
        bad.append(("internet", 0))
        print("NO     the internet (https://github.com) from the sandbox: is the host firewall blocking Embed's egress proxy?")
    if not metadata_guarded():
        bad.append((METADATA, 80))
        print(f"NO     guard for {METADATA}: Embed's egress proxy can reach the cloud metadata service; rerun install.sh")
    if bad:
        sys.exit(f"{len(bad)} problem(s); set network.sandbox_cidr (and host_ports) (sbx-admin config set network), then reinstall")
    print("isolated: a sandbox reaches only " + ", ".join(map(str, sorted(allowed))) + " on the host")


if __name__ == "__main__":
    main()
