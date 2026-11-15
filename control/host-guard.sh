#!/bin/sh
# sbx-host-guard.service, in the active release (host.py), on any machine (a
# cloud VM or bare metal):
# - The default route's interface accepts only network.public_ports (22 and
#   443) and the ports the certificate needs (80 for an IP's, 53 for the
#   delegated DNS-01 responder: settings.opened_ports), replies and ICMP: the
#   runtime may listen on all interfaces (Embed's orchestrator API on 5008,
#   without authentication). A cloud firewall in front is still good; bare
#   metal often has none. public_ports: all leaves it open.
# - Then the runtime's own rules (python -m sbx_control.runtime guard): for
#   Embed, its egress proxy's limits and the routes to the private services
#   (runtime/embed/network.py).
# Runs at boot and whenever install.sh recreates the runtime.
set -eu
eval "$(venv/bin/python -m sbx_control.settings shell)"
iface=$(ip route show default | awk '{print $5; exit}')
for t in iptables ip6tables; do
  # Ours, from an earlier run.
  for chain in OUTPUT INPUT; do
    $t -w -S $chain | grep -E -- '-j SBX-PUBLIC' | sed 's/^-A /-D /' |
      while read -r rule; do eval "$t -w $rule"; done
  done
  $t -w -N SBX-PUBLIC 2>/dev/null || $t -w -F SBX-PUBLIC
  if [ $t = iptables ]; then icmp=icmp; else icmp=ipv6-icmp; fi
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
venv/bin/python -m sbx_control.runtime guard
