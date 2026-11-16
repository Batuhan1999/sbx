#!/bin/sh
# Set up the sandbox platform on this machine, or upgrade it: E2B Embed,
# sbx-control, nginx with a wildcard certificate, the host firewall, the
# agent connectors' services and the base templates. Every step is safe to run
# again; running it again upgrades to the platform this script comes from.
#
#   sudo sh install.sh                  # browser-guided setup, no domain required
#   sudo sh install.sh --domain sbx.example.com --email you@example.com [options]
#
# Options:
#   --domain D        previews at <name>.D, sbx-control at login.D (optional)
#   --email E         for Let's Encrypt when configuring a domain
#   --dns P           how the wildcard certificate is issued: delegated (the
#                     default: one NS record hands _acme-challenge.D to this
#                     host, which answers itself; no credential), route53 (an
#                     instance role, or AWS keys: sbx-admin secret set),
#                     cloudflare (CLOUDFLARE_API_TOKEN), or none: self-signed,
#                     for a first test
#   --login L         password (the default with --domain on a first install;
#                     generated and printed): one shared password that admits
#                     the admins (and sign_in.allowed_emails) only. Whoever
#                     holds it can sign in as any of them, admins included:
#                     for a team, use oidc. none (no sign-in: everyone is an
#                     admin; a private network only), or oidc, connected with
#                     sbx-admin connect sign_in method=oidc oidc_issuer=...
#                     allowed_emails='*@example.com' ... (OIDC admits only
#                     whom allowed_emails names; * for anyone)
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
# Needs Ubuntu 24.04 with /dev/kvm: bare metal, or a VM with nested
# virtualization enabled. With --domain D: A records for D and *.D pointing
# here, and for --dns delegated also the record `_acme-challenge.D NS D.` and
# inbound UDP and TCP port 53 (this host answers the certificate's DNS
# challenge). Without a domain: inbound TCP port 80 for the IP certificate.
# The public interface accepts only SSH, HTTPS and what the certificate needs
# (network.public_ports: sbx-admin config get network); open the same in a
# cloud firewall.
set -eu

domain="" email="" dns="" sign_in="" repos="" public_repos="" admin="" hugepages="" internal="" skip_templates=""
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
    -h|--help) sed -n '2,/^set -eu/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown option $1 (--help)"; exit 1 ;;
  esac
done
# Run from cloud-init or SSM there is no HOME, and some installers need one.
export HOME="${HOME:-/root}"
# Ubuntu's needrestart would print a report after every apt-get.
export NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
say() { printf '\n==> %s\n' "$*"; }
# Installed before: the settings are in sbx.db.
configured() { [ -f /var/lib/sbx/sbx.db ]; }
die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }
[ -n "$public_repos" ] && [ -n "$internal" ] && \
  die "--public-repos and --internal can't go together: Embed lets every sandbox reach the internal ranges, untrusted repos' too"

src=$(cd "$(dirname "$0")" && pwd)
[ -f "$src/control/host-install.sh" ] || die "run install.sh from a release archive or a checkout of the platform"
# A release archive carries the web app's bundle; a checkout must build it.
[ -f "$src/control/sbx_control/web_dist/index.html" ] || \
  die "the web app is not built in $src: install from a release archive (README, Install), or build it first with Node 22: npm --prefix web ci && npm --prefix web run build"

say "Preflight"
[ "$(id -u)" = 0 ] || die "run as root (sudo)"
. /etc/os-release
[ "$ID" = ubuntu ] && [ "$VERSION_ID" = 24.04 ] || die "Ubuntu 24.04 is required, this is $PRETTY_NAME"
[ -e /dev/kvm ] || die "no /dev/kvm: use bare metal or a VM with nested virtualization enabled"
[ -e /dev/net/tun ] || die "no /dev/net/tun"
mem_mib=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
[ "$mem_mib" -ge 15000 ] || die "16 GiB of memory at least, this has $mem_mib MiB"
# An upgrade keeps the count Embed runs with: a new one recreates its runtime.
[ -n "$hugepages" ] || ! configured || hugepages=$(sed -n 's/^HUGEPAGES=//p' /opt/e2b/.env 2>/dev/null | tail -1)
if [ -z "$hugepages" ] && [ "$mem_mib" -lt 23000 ]; then
  die "the default budget needs 24 GiB RAM (12 GiB host reserve, the rest for sandboxes at 4 GiB each unless a recipe asks for more); use a larger host or choose --hugepages explicitly after budgeting host services"
fi
# The memory sandboxes get: everything but 12 GiB for the host, sbx-control
# and Embed's own services, in 2 MiB pages.
[ -n "$hugepages" ] || hugepages=$(( (mem_mib - 12288) / 2 ))
[ "$hugepages" -ge 2048 ] || hugepages=2048
disk_gib=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
# A first install needs room for Embed's images and the base templates; an
# upgrade only for what changes.
need_gib=60; ! configured || need_gib=15
[ "$disk_gib" -ge "$need_gib" ] || die "$need_gib GiB of free disk at least, this has $disk_gib GiB"
first=""; configured || first=1
# A first install defaults the admin to --email and, with a domain, signs in
# with a generated password, which admits the admins only: it needs one.
if [ -n "$first" ]; then
  [ -n "$admin" ] || admin=$email
  [ -n "$sign_in" ] || [ -z "$domain" ] || sign_in=password
  [ "$sign_in" != password ] || [ -n "$admin" ] || die "password sign-in admits the admins only: give --admin EMAIL (or --email)"
fi
echo "ok: $PRETTY_NAME, $mem_mib MiB, $disk_gib GiB free, /dev/kvm"

say "Packages and Docker"
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=300 update -qq
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
if configured; then
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
  password)  # the wizard's too (sign_in.use_password); generates one when none is set, and says it once
    said=$(release_admin sign-in password) || die "could not set password sign-in (above)"
    password=$(printf '%s\n' "$said" | sed -n 's/^generated password: //p') ;;
  oidc)  # refused until the provider and who may sign in (allowed_emails) are named
    release_admin config set sign_in '{"method": "oidc"}' >/dev/null 2>&1 || \
      echo "OIDC is not connected yet: sbx-admin connect sign_in method=oidc oidc_issuer=URL oidc_client_id=ID oidc_client_secret=- allowed_emails='*@example.com'" ;;
  *) section sign_in "$(fields method="$sign_in")" ;;
esac
[ -z "${CLOUDFLARE_API_TOKEN:-}" ] || printf '%s' "$CLOUDFLARE_API_TOKEN" | release_admin secret set CLOUDFLARE_API_TOKEN >/dev/null
echo "settings in sbx.db (sbx-admin config get)"
# Commands the catalog's packages run on this host (their host_tools).
release_host tools >> /tmp/sbx-install.log 2>&1 || { tail -30 /tmp/sbx-install.log; die "could not install the catalog's host tools"; }

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

say "Sandbox runtime credentials: /etc/sbx/runtime.env"
# This host's sandbox runtime: E2B's credentials, made here by Embed, in
# runtime.env (never backed up). Header routing in client-proxy takes an IP
# host, not localhost.
umask 077
cat > /etc/sbx/runtime.env <<ENV
# This host's sandbox runtime (E2B Embed). Written by install.sh; not backed up.
E2B_API_KEY=$key
E2B_API_URL=http://127.0.0.1:3000
E2B_SANDBOX_URL=http://127.0.0.1:3002
ENV
umask 022

say "sbx-control, the host's guard, nginx, connector services"
SBX_UPGRADE_BACKUP=done sh "$release/host-install.sh" >> /tmp/sbx-install.log 2>&1 || { tail -30 /tmp/sbx-install.log; die "see /tmp/sbx-install.log"; }
die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }   # this release runs now: nothing to roll back to
echo "ok: sbx-control $version"

# Managed secret storage is part of a normal installation (infisical/enable.sh).
sh /opt/sbx-control/current/infisical/enable.sh "$store_admin"

if [ -z "$skip_templates" ]; then
  cd /opt/sbx-control/current
  # The step's title says what rebuilds, so the upgrade's progress shows it.
  say "$(venv/bin/python -m sbx_control.template_builds summary 2>/dev/null || echo "Base templates (sbx-base, then default)")"
  venv/bin/python -m sbx_control.template_builds --if-stale base > /tmp/sbx-template-base.log 2>&1 || { tail -20 /tmp/sbx-template-base.log; die "see /tmp/sbx-template-base.log"; }
  venv/bin/python -m sbx_control.template_builds --if-stale default > /tmp/sbx-template-default.log 2>&1 || { tail -20 /tmp/sbx-template-default.log; die "see /tmp/sbx-template-default.log"; }
  echo "ok"
fi

SBX_PASSWORD=$password SBX_ROLLBACK=${rollback:-} sh /opt/sbx-control/current/install-report.sh
