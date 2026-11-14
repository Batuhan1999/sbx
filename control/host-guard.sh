#!/bin/sh
# sbx-host-guard.service, in the active release (host.py), on any machine (a
# cloud VM or bare metal):
# - Sandboxes' TCP leaves through Embed's egress proxy in the orchestrator's
#   container, i.e. from this host itself, so per-sandbox rules and the IMDS
#   hop limit don't apply to it. For that container's cgroup only: no cloud
#   metadata service, and none of this host's own addresses but loopback (the
#   orchestrator's own) and network.host_ports (443: nginx and the gate). On
#   bare metal the public address is on an interface, and the proxy would
#   otherwise reach every port the host listens on.
# - The default route's interface accepts only network.public_ports (22 and
#   443) and the ports the certificate needs (80 for an IP's, 53 for the
#   delegated DNS-01 responder: settings.opened_ports), replies and ICMP: Embed listens on all interfaces, its orchestrator
#   API on 5008 without authentication. A cloud firewall in front is still
#   good; bare metal often has none. public_ports: all leaves it open.
# - Then the routes to the private services (host_network.py).
# Runs at boot and whenever install.sh recreates Embed's containers (the
# orchestrator's cgroup changes with its container).
set -eu
eval "$(venv/bin/python -m sbx_control.settings shell)"
# Docker Compose names the container <project>-<service>-1; the project is /opt/e2b.
cgroup="system.slice/docker-$(docker inspect -f '{{.Id}}' e2b-orchestrator-1).scope"
iface=$(ip route show default | awk '{print $5; exit}')
for t in iptables ip6tables; do
  # Ours, from an earlier run.
  for chain in OUTPUT INPUT; do
    $t -w -S $chain | grep -E -- '-j SBX-(EGRESS|PUBLIC)' | sed 's/^-A /-D /' |
      while read -r rule; do eval "$t -w $rule"; done
  done
  for chain in SBX-EGRESS SBX-PUBLIC; do $t -w -N $chain 2>/dev/null || $t -w -F $chain; done
  if [ $t = iptables ]; then lo=127.0.0.0/8 icmp=icmp; else lo=::1/128 icmp=ipv6-icmp; fi
  [ $t = iptables ] && $t -w -A SBX-EGRESS -d 169.254.169.254/32 -j REJECT
  for port in $host_ports; do $t -w -A SBX-EGRESS -p tcp --dport "$port" -m addrtype --dst-type LOCAL -j RETURN; done
  $t -w -A SBX-EGRESS -p tcp -m addrtype --dst-type LOCAL ! -d $lo -j REJECT --reject-with tcp-reset
  $t -w -I OUTPUT -m cgroup --path "$cgroup" -j SBX-EGRESS
  if [ "$public_ports" != all ] && [ -n "$iface" ]; then
    $t -w -A SBX-PUBLIC -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    $t -w -A SBX-PUBLIC -p $icmp -j RETURN
    $t -w -A SBX-PUBLIC -p udp --dport 68 -j RETURN    # DHCP
    $t -w -A SBX-PUBLIC -p udp --dport 546 -j RETURN   # DHCPv6
    for port in $public_ports; do
      $t -w -A SBX-PUBLIC -p tcp --dport "$port" -j RETURN
      [ "$port" != 53 ] || $t -w -A SBX-PUBLIC -p udp --dport 53 -j RETURN   # DNS (the ACME responder) is UDP first
    done
    $t -w -A SBX-PUBLIC -j DROP
    $t -w -I INPUT -i "$iface" -j SBX-PUBLIC
  fi
done
venv/bin/python -m sbx_control.host_network
