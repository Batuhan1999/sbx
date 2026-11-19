#!/bin/sh
# Set up the sandbox platform on this machine, or upgrade it: the sandbox
# runtimes (E2B Embed where this host can run it, E2B Cloud with an API key),
# sbx-control, nginx with a wildcard certificate, the host firewall, the
# agent connectors' services and the base templates. Every step is safe to run
# again; running it again upgrades to the platform this script comes from.
#
#   sudo sh install.sh                  # browser-guided setup, no domain required
#   sudo sh install.sh --domain sbx.example.com --email you@example.com [options]
#   sudo --preserve-env=E2B_CLOUD_API_KEY sh install.sh --domain sbx.example.com ...
#                                       # sandboxes at E2B Cloud: a small host, no KVM
#   sudo sh install.sh --check          # this host's facts and the runtimes it can
#                                       # run; installs and changes nothing
#
# Runtimes: a first install enables E2B Embed (Firecracker on this host)
# when the host can run it: /dev/kvm, 16 GiB of memory (24 for the default
# budget) and 60 GiB of free disk. With E2B_CLOUD_API_KEY in the environment
# it also enables E2B Cloud (the key goes to /etc/sbx/runtime.env), the
# default where Embed is off; such a host needs about 2 GiB of memory and
# 10 GiB of disk, and a domain (E2B's sandboxes call this server back at
# login.D). A host that can run neither installs with no runtime enabled
# (SBX_RUNTIMES empty): add one on the setup page's Sandboxes step or Settings → Sandbox
# runtimes, or with sbx-admin runtime add e2b-cloud. An upgrade keeps the
# runtimes the installation enables (SBX_RUNTIMES); a key given again
# replaces E2B Cloud's or enables it. Later: sbx-admin runtime add, remove,
# default, or the web app.
#
# Options:
#   --domain D        previews at <name>.D, sbx-control at app.D (optional)
#   --email E         the first admin unless --admin names one; also the Let's
#                     Encrypt account's contact (optional: it sends no expiry emails)
#   --dns P           how the wildcard certificate is issued: delegated (the
#                     default: one NS record hands _acme-challenge.D to this
#                     host, which answers itself; no credential), route53 (an
#                     instance role, or AWS keys: sbx-admin secret set),
#                     cloudflare (CLOUDFLARE_API_TOKEN), or none: self-signed,
#                     for a first test
#   --login L         password (the default with --domain on a first install):
#                     each person has their own; the admins get a generated
#                     one, printed once. Add people in the web app (Settings
#                     → People) or with sbx-admin user add EMAIL. none (no
#                     sign-in: everyone is an admin; a private network only),
#                     or oidc, connected with sbx-admin connect sign_in
#                     method=oidc oidc_issuer=... allowed_emails='*@example.com'
#                     ... (OIDC admits the admins, the people added, and whom
#                     allowed_emails names; * for anyone)
#   --repos LIST      trusted repos, comma-separated: owner/repo or owner/*
#                     (their secrets, services, private network)
#   --public-repos    also any other repo, public ones included, untrusted
#                     (not with --internal: Embed lets every sandbox reach those)
#   --admin EMAIL     may set secrets and build templates (default on a first
#                     install: --email)
#   --hugepages N     2 MiB pages for sandbox memory (default: from RAM on a
#                     first install; an upgrade keeps the installed count)
#   --internal CIDRS  private ranges sandboxes may reach (default: none; the
#                     networks of sbx-admin server up are added on their own)
#   --skip-templates  don't build sbx-base and default now
#   --check           print the host's facts (OS, CPU, memory, disk, /dev/kvm,
#                     public IP, ports 22/53/80/443 in use, an installation
#                     here) and which runtimes it can run, then stop; exit 1
#                     when sbx can't be installed here. Needs no root (with
#                     sudo it also names the processes on those ports).
#
# The settings options (--domain, --email, --dns, --login, --repos,
# --public-repos, --admin, --internal) are written on a first install only
# (no /var/lib/sbx/sbx.db yet). On an upgrade they are ignored, with a note:
# the installation keeps its settings, including what changed after install
# (repos added in the web app, ranges, admins, sign-in). Change them with
# sbx-admin config set SECTION JSON or sbx-admin connect. --hugepages and
# --skip-templates apply on every run.
#
# An upgrade refuses, before it changes anything, when it would recreate
# Embed's running services (a new runtime pin, sbx's compose override, a key
# sbx owns in /opt/e2b/.env) while sandboxes exist: they do not survive that,
# paused ones included. Delete them first.
#
# An upgrade stops sbx-control and the agent connectors' host services before
# it migrates sbx.db, after backing up the state into
# /var/lib/sbx/upgrade-backups/<time>/ (sbx.db, the unit files and a backup
# bundle); `sh .../rollback.sh` there goes back to the release before. They
# start again with the new release; running sandboxes keep running.
#
# Needs Ubuntu 24.04; for E2B Embed also /dev/kvm: bare metal, or a VM with
# nested virtualization enabled. With --domain D: A records for D and *.D pointing
# here, and for --dns delegated also the record `_acme-challenge.D NS D.` and
# inbound UDP and TCP port 53 (this host answers the certificate's DNS
# challenge). Without a domain: inbound TCP port 80 for the IP certificate,
# which the installer gets (on a public IP, while the setup is open and has
# no address yet) before it prints the onboarding address.
# The public interface accepts only SSH, HTTPS and what the certificate needs
# (network.public_ports: sbx-admin config get network); open the same in a
# cloud firewall.
#
# While the first-run setup is open, the last line is the onboarding page,
# `==> Open the onboarding: https://ADDRESS/onboarding` (no credential: its
# first step makes the first admin's account); the line before it says when
# the browser will warn. An installation that is set up
# prints its address instead.
set -eu

domain="" email="" dns="" sign_in="" repos="" public_repos="" admin="" hugepages="" internal="" skip_templates="" check=""
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) domain=$2; shift 2 ;;
    --email) email=$2; shift 2 ;;
    --dns) dns=$2; shift 2 ;;
    --login) sign_in=$2; shift 2 ;;
    --repos) repos=$2; shift 2 ;;
    --public-repos) public_repos=true; shift ;;
    --admin) admin=$2; shift 2 ;;
    --hugepages) hugepages=$2; shift 2 ;;
    --internal) internal=$2; shift 2 ;;
    --skip-templates) skip_templates=1; shift ;;
    --check) check=1; shift ;;
    -h|--help) sed -n '2,/^set -eu/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown option $1 (--help)"; exit 1 ;;
  esac
done
# E2B Cloud's key stays in this shell, out of every command's environment
# but the one that writes it into runtime.env (below).
cloud_key=${E2B_CLOUD_API_KEY:-} cloud_given=""
[ -z "$cloud_key" ] || cloud_given=1
unset E2B_CLOUD_API_KEY
# Run from cloud-init or SSM there is no HOME, and some installers need one.
export HOME="${HOME:-/root}"
# Ubuntu's needrestart would print a report after every apt-get.
export NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
say() { printf '\n==> %s\n' "$*"; }
# Installed before: the settings are in sbx.db.
configured() { [ -f /var/lib/sbx/sbx.db ]; }
die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }
[ -n "$public_repos" ] && [ -n "$internal" ] && \
  die "--public-repos and --internal can't go together: the internal ranges are open to every sandbox, untrusted repos' too"

# --check: what this host is and which runtimes it can run, from the facts
# and the decision below; it changes nothing. Exit 1 when sbx can't install here.
check_report() {
  verdict=0
  say "Host check (nothing is changed)"
  if [ -n "$ubuntu" ]; then echo "os:         $os"; else echo "os:         $os: sbx needs Ubuntu 24.04"; verdict=1; fi
  virt=$(systemd-detect-virt 2>/dev/null || true)   # "none" on bare metal
  case "${virt:-}" in
    "") machine="unknown" ;;
    none) machine="bare metal" ;;
    *) machine="a VM ($virt)" ;;
  esac
  echo "machine:    $machine, $(nproc 2>/dev/null || echo '?') CPUs${vmx:+, hardware virtualization (vmx/svm)}"
  echo "memory:     $mem_mib MiB"
  echo "disk:       $disk_gib GiB free on /"
  if [ -n "$kvm" ]; then echo "/dev/kvm:   yes"
  elif [ -n "$vmx" ]; then echo "/dev/kvm:   no; the CPU has virtualization: load its module (modprobe kvm_intel or kvm_amd)"
  elif [ "${virt:-none}" != none ]; then echo "/dev/kvm:   no; a VM without nested virtualization: enable it at the provider, or use bare metal"
  else echo "/dev/kvm:   no"
  fi
  ip=""; ! command -v curl >/dev/null || ip=$(curl -fsS --max-time 5 https://checkip.amazonaws.com 2>/dev/null || true)
  echo "public IP:  ${ip:-unknown (no outbound HTTPS?)}"
  for port in 22 53 80 443; do
    line=$(ss -Hltunp "( sport = :$port )" 2>/dev/null | head -1 || true)
    if [ -z "$line" ]; then printf '%-12sfree\n' "port $port:"; continue; fi
    by=$(printf '%s\n' "$line" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p')
    at=$(printf '%s\n' "$line" | awk '{print $5}')
    printf '%-12sin use%s on %s\n' "port $port:" "${by:+ by $by}" "$at"
  done
  if configured || [ -n "$has_env" ]; then echo "sbx:        installed (runtimes: ${installed:-none}); install.sh upgrades it"
  else echo "sbx:        not installed"
  fi
  echo "runtimes this host can run:"
  if [ -z "$unfit" ]; then printf '  %-10s yes: sandboxes on this server\n' embed; else printf '  %-10s no: %s\n' embed "$unfit"; fi
  small=""
  [ "$mem_mib" -ge 1800 ] || small="2 GiB of memory at least, this has $mem_mib MiB"
  [ -n "$small" ] || [ "$disk_gib" -ge 10 ] || small="10 GiB of free disk at least, this has $disk_gib GiB"
  for provider in "e2b-cloud:an E2B API key" "boat:a Boat API key (a paid plan: the trial saves no templates)"; do
    if enables "${provider%%:*}"; then printf '  %-10s yes: enabled\n' "${provider%%:*}"
    elif [ -n "$small" ]; then printf '  %-10s no: %s\n' "${provider%%:*}" "$small"
    else printf '  %-10s yes: needs %s and a domain pointing here\n' "${provider%%:*}" "${provider#*:}"
    fi
  done
  [ -z "$small" ] || verdict=1
  if [ -n "$embed" ] && [ -n "$unfit" ]; then
    echo "verdict:    the upgrade refuses: this installation enables embed, and $unfit"; return 1
  fi
  if [ "$verdict" != 0 ]; then echo "verdict:    sbx can't be installed on this host"
  elif [ "$installed" = unknown ]; then echo "verdict:    installed; install.sh upgrades it (with sudo, --check names its runtimes)"
  elif [ -n "$installed" ]; then echo "verdict:    installed with $installed; install.sh upgrades it"
  elif [ -z "$unfit" ]; then echo "verdict:    embed (a provider runtime can be added too)"
  else echo "verdict:    a provider runtime (e2b-cloud or boat): it needs the owner's API key and a domain"
  fi
  return "$verdict"
}

src=$(cd "$(dirname "$0")" && pwd)
if [ -z "$check" ]; then
  [ -f "$src/control/host-install.sh" ] || die "run install.sh from a release archive or a checkout of the platform"
  # A release archive carries the web app's bundle; a checkout must build it.
  [ -f "$src/control/sbx_control/web_dist/index.html" ] || \
    die "the web app is not built in $src: install from a release archive (README, Install), or build it first with Node 22: npm --prefix web ci && npm --prefix web run build"
  say "Preflight"
  [ "$(id -u)" = 0 ] || die "run as root (sudo)"
fi
# The host's facts, as the preflight and --check read them.
ID="" VERSION_ID="" PRETTY_NAME="$(uname -s)"
[ ! -r /etc/os-release ] || . /etc/os-release
os=$PRETTY_NAME ubuntu=""
[ "$ID" != ubuntu ] || [ "$VERSION_ID" != 24.04 ] || ubuntu=1
[ -n "$check" ] || [ -n "$ubuntu" ] || die "Ubuntu 24.04 is required, this is $PRETTY_NAME"
mem_mib=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo 2>/dev/null || true); mem_mib=${mem_mib:-0}
disk_gib=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc 0-9); disk_gib=${disk_gib:-0}
kvm="" tun="" vmx=""
[ ! -e /dev/kvm ] || kvm=1
[ ! -e /dev/net/tun ] || tun=1
! grep -Eqw 'vmx|svm' /proc/cpuinfo 2>/dev/null || vmx=1
first=""; configured || first=1
# The runtimes this installation enables now (SBX_RUNTIMES in runtime.env,
# missing: Embed alone; present and empty: none yet); none before a first
# install writes the file. An installation without the file runs Embed, as
# sbx reads it: never an empty selection written over it.
installed="" has_env=""
reader=$src/control/sbx_control/runtime/env_file.py
if [ -n "$check" ] && [ -e /etc/sbx ] && { [ "$(id -u)" != 0 ] || [ ! -f "$reader" ]; }; then
  # --check without root (runtime.env is root-only) or install.sh alone (no reader): not read.
  installed=unknown has_env=1
elif [ -f /etc/sbx/runtime.env ]; then
  has_env=1
  # As sbx reads it: its own reader, before a release is unpacked.
  installed=$(python3 -I "$reader" runtimes /etc/sbx/runtime.env) ||
    die "could not read /etc/sbx/runtime.env (above)"
elif configured; then
  installed=embed
fi
enables() { case " $installed " in *" $1 "*) return 0 ;; esac; return 1; }
# Why E2B Embed can't run on this host ("" when it can).
unfit=""
[ -n "$kvm" ] || unfit="no /dev/kvm: use bare metal or a VM with nested virtualization enabled"
[ -n "$unfit" ] || [ -n "$tun" ] || unfit="no /dev/net/tun"
[ -n "$unfit" ] || [ "$mem_mib" -ge 15000 ] || unfit="16 GiB of memory at least, 12 for the host and 4 for one sandbox; this has $mem_mib MiB"
# An upgrade keeps the count Embed runs with: a new one recreates its runtime.
[ -n "$hugepages" ] || ! configured || hugepages=$(sed -n 's/^HUGEPAGES=//p' /opt/e2b/.env 2>/dev/null | tail -1)
if [ -z "$unfit" ] && [ -z "$hugepages" ] && [ "$mem_mib" -lt 23000 ]; then
  unfit="the default budget needs 24 GiB RAM (12 GiB host reserve, the rest for sandboxes at 4 GiB each unless a recipe asks for more); use a larger host or choose --hugepages explicitly after budgeting host services"
fi
# A first install needs room for Embed's images and the base templates; an
# upgrade only for what changes.
need_gib=60; ! configured || need_gib=15
[ -n "$unfit" ] || [ "$disk_gib" -ge "$need_gib" ] || unfit="$need_gib GiB of free disk at least, this has $disk_gib GiB"
# Embed: where the installation enables it already (it must still fit), or
# on a first install (no runtime.env yet) where the host can run it. An
# installation that enables none keeps it so: runtime.env changes only by an
# explicit change (the web app, sbx-admin runtime, a key given here).
embed=""
if [ -n "$installed" ]; then
  # --check reports this refusal (check_report) instead of stopping before its report.
  if enables embed; then embed=1; [ -z "$unfit" ] || [ -n "$check" ] || die "$unfit"; fi
elif [ -z "$has_env" ] && [ -z "$unfit" ]; then
  embed=1
fi
# E2B Cloud: with its key in the environment, or enabled already.
cloud=""
{ [ -n "$cloud_key" ] || enables e2b-cloud; } && cloud=1
if [ -n "$check" ]; then check_report && exit 0; exit 1; fi
# Neither: sbx installs with no runtime enabled; an admin adds a provider's
# on the setup page (it needs a domain: its sandboxes call this server back).
none=""
if [ -z "$embed" ] && [ -z "$cloud" ] && [ -z "$installed" ]; then
  none=1
  if [ -n "$has_env" ]; then
    echo "no sandbox runtime is enabled (SBX_RUNTIMES is empty): add one in Settings → Sandbox runtimes"
  else
    echo "E2B Embed: off on this host ($unfit); no provider runtime is configured: sbx installs with no sandbox runtime"
  fi
fi
if [ -z "$embed" ]; then
  # The sandboxes run at a provider: this host runs sbx-control, nginx and the secret store.
  [ -z "$unfit" ] || [ -n "$installed" ] || [ -n "$none" ] || echo "E2B Embed: off on this host ($unfit)"
  [ "$mem_mib" -ge 1800 ] || die "2 GiB of memory at least, this has $mem_mib MiB"
  [ "$disk_gib" -ge 10 ] || die "10 GiB of free disk at least, this has $disk_gib GiB"
else
  # The memory sandboxes get: everything but 12 GiB for the host, sbx-control
  # and Embed's own services, in 2 MiB pages.
  [ -n "$hugepages" ] || hugepages=$(( (mem_mib - 12288) / 2 ))
  [ "$hugepages" -ge 2048 ] || hugepages=2048
fi
# A provider's sandboxes call this server back at login.D (bundles, git
# credentials, catalog actions): a first install with one needs the domain.
[ -z "$cloud" ] || [ -z "$first" ] || [ -n "$domain" ] || \
  die "E2B Cloud's sandboxes call this server back at login.D: give --domain D (A records for D and *.D pointing here)"
# A first install defaults the admin to --email and, with a domain, signs in
# with a generated password for the admin: it needs one.
if [ -n "$first" ]; then
  [ -n "$admin" ] || admin=$email
  [ -n "$sign_in" ] || [ -z "$domain" ] || sign_in=password
  [ "$sign_in" != password ] || [ -n "$admin" ] || die "password sign-in needs an admin to sign in: give --admin EMAIL (or --email)"
fi
echo "ok: $PRETTY_NAME, $mem_mib MiB, $disk_gib GiB free; runtimes:${embed:+ embed (/dev/kvm)}${cloud:+ e2b-cloud}${installed:+ (enabled: $installed)}"

say "Packages and Docker"
export DEBIAN_FRONTEND=noninteractive
# DPkg::Lock::Timeout waits for dpkg's lock, not for the package lists' one,
# which a new server's own first-boot update holds for a few minutes.
tries=0
until apt-get -o DPkg::Lock::Timeout=300 update -qq; do
  tries=$((tries + 1))
  [ "$tries" -lt 30 ] || die "apt-get update kept failing for five minutes (see above)"
  echo "apt-get update failed; another apt run may hold the package lists. Trying again in 10 s."
  sleep 10
done
apt-get -o DPkg::Lock::Timeout=300 install -y -qq ca-certificates curl git iptables rsync e2fsprogs iproute2 \
  python3-venv jq openssl xfsprogs >/dev/null
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh >/dev/null 2>&1
systemctl enable --now docker >/dev/null 2>&1

say "sbx-control release"
# A new directory per install (sbx_control/host.py: the layout); the running
# release stays untouched until host-install.sh switches to this one.
# Its name: VERSION in a release archive, else the checkout's nearest tag
# (v0.2.0, or v0.2.0-3-g<commit> on main past it).
if [ -f "$src/VERSION" ]; then version=$(cat "$src/VERSION")
else version=$(git -c safe.directory="$src" -C "$src" describe --tags --always 2>/dev/null || echo local)
fi
release=/opt/sbx-control/releases/$(date -u +%Y%m%d-%H%M%S)-$version
sh "$src/control/release.sh" "$release" "$version" >/dev/null
sh "$release/host-install.sh" --prepare > /tmp/sbx-install.log 2>&1 || { tail -30 /tmp/sbx-install.log; die "see /tmp/sbx-install.log"; }
# The new release's own commands, before it is the active one.
release_admin() { (cd "$release" && venv/bin/python -m sbx_control.admin "$@"); }
release_host() { (cd "$release" && venv/bin/python -m sbx_control.host "$@"); }
release_embed() { (cd "$release" && venv/bin/python -m sbx_control.runtime.embed "$@"); }
echo "ok: $release"
if configured && [ -n "$embed" ]; then
  # Before an upgrade stops or changes anything: what it lays into Embed's
  # project must not recreate the runtime under sandboxes.
  said=$(release_embed upgrade-check --source "$src" --hugepages "$hugepages" 2>&1) || die "$said Nothing was changed."
  [ -z "$said" ] || echo "$said"
fi

say "Settings: /etc/sbx"
install -d -m 700 /etc/sbx /etc/sbx/apps
if configured; then
  # An upgrade: sbx-control and the connectors' services stop here, after a
  # backup of the state (/var/lib/sbx/upgrade-backups), and start again with
  # this release below. Running sandboxes keep running.
  echo "upgrade: stopping sbx-control and its connectors' services until this release starts"
  sh "$release/host-install.sh" --migrate >> /tmp/sbx-install.log 2>&1 || { tail -30 /tmp/sbx-install.log; die "the upgrade's backup or migration failed; see /tmp/sbx-install.log"; }
  grep -E '^(upgrade backup|rollback):' /tmp/sbx-install.log || true
  rollback=$(sed -n 's/^rollback: sh //p' /tmp/sbx-install.log | tail -1)
  # Until this release runs, a failure goes back to the one that ran (its
  # release, unit files and sbx.db) and starts sbx-control and the
  # connectors again: a failed Embed start must not leave previews down.
  if [ -n "$rollback" ]; then
    die() {
      printf '\nFAILED: %s\n' "$*" >&2
      echo "rolling back to the release that ran before: sh $rollback" >&2
      sh "$rollback" >&2 || echo "the rollback failed too; run it by hand: sh $rollback" >&2
      exit 1
    }
  fi
else
  release_admin migrate >/dev/null || die "could not prepare /var/lib/sbx/sbx.db (above)"
fi
# A first install's options, as settings, before anything reads them (Embed's
# .env below, the host's units); sbx-admin validates them and merges each
# into its section. An upgrade keeps the settings as they are (--help).
list() { python3 -c 'import json, sys; print(json.dumps([x.strip() for x in sys.argv[1].split(",") if x.strip()]))' "$1"; }
fields() { python3 -c 'import json, sys; print(json.dumps({k: v for k, _, v in (a.partition("=") for a in sys.argv[1:]) if v}))' "$@"; }
section() { release_admin config set "$1" "$2" >/dev/null || die "could not set $1 (above)"; }
store_admin=${admin:-${email:-admin@sbx.invalid}}   # Infisical's first admin, when its store is set up below
given=""
for flag in "domain:$domain" "email:$email" "dns:$dns" "login:$sign_in" "repos:$repos" "public-repos:$public_repos" \
    "admin:$admin" "internal:$internal"; do
  [ -z "${flag#*:}" ] || given="$given --${flag%%:*}"
done
if [ -z "$first" ] && [ -n "$given" ]; then
  echo "upgrade: kept the installation's settings; ignored$given (change them with sbx-admin config set or sbx-admin connect)"
  domain="" email="" dns="" sign_in="" repos="" public_repos="" admin="" internal=""
fi
[ -z "$repos" ] || section repos "$(list "$repos")"
[ -z "$admin" ] || section access "{\"admins\": $(list "$admin")}"
[ -z "$public_repos" ] || section access '{"public_repos": true}'
[ -z "$internal" ] || section network "{\"internal_cidrs\": $(list "$internal")}"
[ -z "$domain$email$dns" ] || section domain "$(fields name="$domain" acme_email="$email" acme="$dns")"
password=""
case "$sign_in" in
  "") ;;
  password)  # a generated password for each admin without one, said once
    said=$(release_admin sign-in password) || die "could not set password sign-in (above)"
    password=$(printf '%s\n' "$said" | sed -n 's/^generated password for //p') ;;
  oidc)  # refused until the provider and who may sign in (allowed_emails) are named
    release_admin config set sign_in '{"method": "oidc"}' >/dev/null 2>&1 || \
      echo "OIDC is not connected yet: sbx-admin connect sign_in method=oidc oidc_issuer=URL oidc_client_id=ID oidc_client_secret=- allowed_emails='*@example.com'" ;;
  *) section sign_in "$(fields method="$sign_in")" ;;
esac
[ -z "${CLOUDFLARE_API_TOKEN:-}" ] || printf '%s' "$CLOUDFLARE_API_TOKEN" | release_admin secret set CLOUDFLARE_API_TOKEN >/dev/null
echo "settings in sbx.db (sbx-admin config get)"
# Commands the catalog's packages run on this host (their host_tools).
release_host tools >> /tmp/sbx-install.log 2>&1 || { tail -30 /tmp/sbx-install.log; die "could not install the catalog's host tools"; }

# The settings an upgrade keeps: a provider runtime needs the domain (above).
eval "$(cd "$release" && venv/bin/python -m sbx_control.settings shell | grep -E '^(domain|dns)=' | sed 's/^/setting_/')"
[ -z "$cloud" ] || [ -n "${setting_domain:-}" ] || \
  die "E2B Cloud's sandboxes call this server back at login.D: connect a domain first (sbx-admin connect domain ...), then run this again"
if [ -n "$cloud" ] && [ "${setting_dns:-}" = none ]; then
  echo "note: --dns none gives a self-signed certificate. sbx's own calls from E2B Cloud sandboxes (template bundles, git credentials) pin its key; anything else in a sandbox that calls this server refuses it. Use a DNS provider for a trusted certificate."
fi

if [ -n "$embed" ]; then
say "E2B Embed"
if [ -z "$first" ]; then
  # Again, now that sbx-control is stopped and creates no sandboxes; a
  # refusal here goes back to the release that ran (die, above).
  said=$(release_embed upgrade-check --source "$src" --hugepages "$hugepages" 2>&1) || die "$said"
fi
install -d -m 0755 /opt/e2b
rsync -a --exclude .env "$src/vendor/e2b-embed/compose/" /opt/e2b/
if [ ! -f /opt/e2b/.env ]; then
  cp "$src/vendor/e2b-embed/compose/.env" /opt/e2b/.env && chmod 600 /opt/e2b/.env
fi
install -m 644 "$src/embed/compose.override.yaml" /opt/e2b/compose.override.yaml
# Every key sbx owns there (the settings', the extensions', the hugepages and
# the platform's image pins) has one writer: sbx_control/runtime/embed/host.py.
release_embed embed-env --hugepages "$hugepages" --pins "$src/vendor/e2b-embed/compose/.env" \
  || die "could not write /opt/e2b/.env (above)"
echo "starting (the first time pulls images and builds Embed's base template: several minutes)"
(cd /opt/e2b && docker compose up -d --wait) > /tmp/e2b-up.log 2>&1 || { tail -30 /tmp/e2b-up.log; die "Embed did not come up; see /tmp/e2b-up.log"; }
key=$(cd /opt/e2b && docker compose exec -T ready cat /run/e2b/sdk.env | sed -n 's/^export E2B_API_KEY=//p')
[ -n "$key" ] || die "Embed gave no API key"
# Embed seeds its team with a hosted free tier: 512 MiB to build in (too
# little for apt or pnpm install) and one-hour sandboxes. Our limits, then
# the api again, which caches them.
(cd /opt/e2b && docker compose exec -T postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 -q) \
  < "$src/embed/tier.sql" >/dev/null || die "could not set Embed's team limits (embed/tier.sql)"
# The api caches a team with its limits in Redis, past a restart: drop it.
(cd /opt/e2b && docker compose exec -T redis sh -c "redis-cli --scan --pattern 'auth:team:*' | xargs -r redis-cli del" \
  >/dev/null && docker compose restart api >/dev/null 2>&1 && docker compose up -d --wait >/dev/null 2>&1) \
  || die "Embed's api did not come back after the limits"
# Recreated containers: the guard follows the orchestrator's new cgroup. On
# a first install host-install.sh starts it, before sbx-control.
! systemctl is-active -q sbx-host-guard || systemctl restart sbx-host-guard || die "could not set up the host's firewall (sbx-host-guard)"
echo "ok: $hugepages hugepages, API on 127.0.0.1:3000"

fi

say "Sandbox runtime credentials: /etc/sbx/runtime.env"
# This host's sandbox runtimes and their keys, in runtime.env (never backed
# up): Embed's, made here by Embed (header routing in client-proxy takes an
# IP host, not localhost), and E2B Cloud's from E2B_CLOUD_API_KEY. A fresh
# install enables those (SBX_RUNTIMES, Embed first: the default); on an
# existing one, everything else (the runtimes enabled, the default, other
# runtimes' keys) stays as the operator set it. Values pass in the
# environment, never on a command line.
runtimes=""
if [ -n "$embed" ]; then
  # Embed's URLs as before; E2B_HOST and E2B_DOMAIN lines go (unset here).
  # Prefix assignments: the key is in that command's environment, not its arguments.
  runtimes=$(cd "$release" && unset E2B_HOST E2B_DOMAIN && E2B_API_KEY="$key" E2B_API_URL=http://127.0.0.1:3000 \
    E2B_SANDBOX_URL=http://127.0.0.1:3002 venv/bin/python -m sbx_control.runtime env --enable embed \
    E2B_API_KEY E2B_API_URL E2B_SANDBOX_URL E2B_HOST E2B_DOMAIN) || die "could not write /etc/sbx/runtime.env (above)"
fi
if [ -n "$cloud_key" ]; then
  runtimes=$(cd "$release" && E2B_CLOUD_API_KEY="$cloud_key" venv/bin/python -m sbx_control.runtime env \
    --enable e2b-cloud E2B_CLOUD_API_KEY) || die "could not write /etc/sbx/runtime.env (above)"
fi
if [ -n "$none" ] && [ -z "$has_env" ]; then
  # A first install names no runtime (SBX_RUNTIMES=''); an existing file is never rewritten here.
  runtimes=$(cd "$release" && venv/bin/python -m sbx_control.runtime env) || die "could not write /etc/sbx/runtime.env (above)"
fi
unset cloud_key
echo "ok: ${runtimes:-runtimes as runtime.env names them}"

say "sbx-control, the host's guard, nginx, connector services"
SBX_UPGRADE_BACKUP=done sh "$release/host-install.sh" >> /tmp/sbx-install.log 2>&1 || { tail -30 /tmp/sbx-install.log; die "see /tmp/sbx-install.log"; }
die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }   # this release runs now: nothing to roll back to
echo "ok: sbx-control $version"

# Managed secret storage is part of a normal installation (infisical/enable.sh).
sh /opt/sbx-control/current/infisical/enable.sh "$store_admin"
# A key given on an upgrade, once the store is Infisical: into it, like the rest.
[ -z "$cloud_given" ] || (cd /opt/sbx-control/current && venv/bin/python -c \
  'from sbx_control import vault; vault.sealing() and vault.seal_runtime_file()') || die "could not seal E2B_CLOUD_API_KEY into the store"

if [ -z "$skip_templates" ]; then
  cd /opt/sbx-control/current
  # The step's title says what rebuilds, so the upgrade's progress shows it.
  say "$(venv/bin/python -m sbx_control.template_builds summary 2>/dev/null || echo "Base templates (sbx-base, then default)")"
  venv/bin/python -m sbx_control.template_builds --if-stale base > /tmp/sbx-template-base.log 2>&1 || { tail -20 /tmp/sbx-template-base.log; die "see /tmp/sbx-template-base.log"; }
  venv/bin/python -m sbx_control.template_builds --if-stale default > /tmp/sbx-template-default.log 2>&1 || { tail -20 /tmp/sbx-template-default.log; die "see /tmp/sbx-template-default.log"; }
  echo "ok"
fi

[ -z "$none" ] || echo "
No sandbox runtime yet: add one on the onboarding page's Sandboxes step (or sbx-admin runtime add e2b-cloud, the API key on stdin)."
# While the first-run setup is open on a server with no address yet: IP
# access with a trusted certificate (Let's Encrypt over port 80), so the
# onboarding page below opens without a browser warning; without it, the
# self-signed one until the setup chooses an address (setup.first_certificate).
setup_open=0
sbx-admin setup-infer >/dev/null || setup_open=$?
if [ "$setup_open" = 3 ]; then
  say "HTTPS for the first visit"
  (cd /opt/sbx-control/current && venv/bin/python -m sbx_control.setup first-certificate) ||
    echo "self-signed for now: could not get the IP certificate (above)"
fi
SBX_PASSWORD=$password SBX_ROLLBACK=${rollback:-} sh /opt/sbx-control/current/install-report.sh
