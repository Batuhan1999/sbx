#!/bin/sh
# Make the local Infisical the managed secret store, as root, from the active
# release: install.sh runs it, and `sbx-admin store enable`. Its unit
# (sbx-infisical) comes from sbx_control/host.py once /etc/sbx/infisical.env
# exists; the scheduled backup bundle (sbx-backup) then carries its database.
# Once Infisical is the store it only brings it up to infisical/compose.yaml
# (the images a release pins; nothing changes when they are the same), so
# upgrades neither stop sbx-control nor take backups; the sandbox runtime
# keeps running.
#   infisical/enable.sh [ADMIN_EMAIL[,MORE...]]   (Infisical's admin: the first)
set -eu
[ "$(id -u)" = 0 ] || { echo 'run as root' >&2; exit 1; }
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
admin=$(printf '%s' "${1:-}" | cut -d, -f1 | tr -d ' ')
[ -n "$admin" ] || admin=admin@sbx.invalid
say() { printf '\n==> %s\n' "$*"; }
die() { printf 'FAILED: %s\n' "$*" >&2; exit 1; }
venv/bin/python -m sbx_control.admin migrate >/dev/null
eval "$(venv/bin/python -c 'from sbx_control import settings; s = settings.current().store
print("port=%d enabled=%d" % (s.port, s.mode == "infisical" and bool(s.project)))')"
say "Infisical (the repos' env, managed secrets on 127.0.0.1:$port)"
export INFISICAL_ENV="${INFISICAL_ENV:-/etc/sbx/infisical.env}" INFISICAL_PORT=$port
# New keys for a store that holds secrets would lose them all.
[ "$enabled" = 0 ] || [ -f "$INFISICAL_ENV" ] || die "Infisical is the store, but $INFISICAL_ENV is missing: restore it from a backup"
if [ ! -f "$INFISICAL_ENV" ]; then
  pg=$(openssl rand -hex 24)
  umask 077
  cat > "$INFISICAL_ENV.new" <<ENV
# Infisical on this host (infisical/enable.sh). ENCRYPTION_KEY and AUTH_SECRET
# protect every secret in its database: back this file up with the database.
ENCRYPTION_KEY=$(openssl rand -hex 16)
AUTH_SECRET=$(openssl rand -base64 32)
POSTGRES_USER=infisical
POSTGRES_PASSWORD=$pg
POSTGRES_DB=infisical
DB_CONNECTION_URI=postgres://infisical:$pg@db:5432/infisical
REDIS_URL=redis://redis:6379
SITE_URL=http://127.0.0.1:$port
ENV
  mv "$INFISICAL_ENV.new" "$INFISICAL_ENV"
  umask 022
fi
compose() { docker compose -f infisical/compose.yaml "$@"; }
# The images first, apart from the start: a fresh host pulls them for minutes, and
# `up --wait` gives up when a service is still unhealthy while the others pull.
# A registry that stumbles is tried again; a slow first start (Postgres made, then
# Infisical's migrations) is waited for again.
SBX_INFISICAL_PAUSE=${SBX_INFISICAL_PAUSE:-15}   # seconds before the next try, times the try's number
pulled=''
for try in 1 2 3; do
  if compose pull --quiet >/dev/null 2>&1; then pulled=1; break; fi
  [ "$try" = 3 ] || { echo "pulling Infisical's images failed (try $try of 3); again shortly" >&2; sleep $((try * SBX_INFISICAL_PAUSE)); }
done
[ -n "$pulled" ] || die "could not pull Infisical's images: docker compose -f $PWD/infisical/compose.yaml pull"
started=''
for try in 1 2 3 4; do
  if compose up -d --wait >/dev/null 2>&1; then started=1; break; fi
  [ "$try" = 4 ] || { echo "Infisical is not healthy yet (try $try of 4); waiting" >&2; sleep $((try * SBX_INFISICAL_PAUSE)); }
done
if [ -z "$started" ]; then
  # What failed, without what the services' environments hold: their states, then their
  # logs' last lines with anything that looks like a credential left out.
  echo "Infisical's containers:" >&2
  compose ps -a >&2 || true
  echo "the last lines of their logs (lines naming credentials left out):" >&2
  compose logs --no-color --tail 40 2>&1 \
    | grep -viE 'password|passwd|secret|token|encryption_key|auth_secret|connection_uri|postgres://|redis://|bearer|api[_-]?key' >&2 || true
  die "Infisical did not start: docker compose -f $PWD/infisical/compose.yaml logs"
fi
curl --fail --silent --output /dev/null --retry 60 --retry-connrefused --retry-all-errors --retry-delay 5 --max-time 10 \
  "http://127.0.0.1:$port/api/status" || die "Infisical did not answer on 127.0.0.1:$port"
[ "$enabled" = 0 ] || { echo "ok: Infisical is the secret store already, on the images infisical/compose.yaml pins"; exit 0; }
control_was_active=$(systemctl is-active sbx-control || true)
trap '[ "$control_was_active" != active ] || systemctl start sbx-control' EXIT
install -d -m 700 /var/lib/sbx/backups
venv/bin/python -m sbx_control.admin backup --out "/var/lib/sbx/backups/before-infisical-$(date +%Y%m%d-%H%M%S).tar.gz" >/dev/null
systemctl stop sbx-control
venv/bin/python -m sbx_control.secret_store.infisical setup "$port" "$admin" \
  || die "could not set Infisical up"
venv/bin/python -m sbx_control.admin backup --out "/var/lib/sbx/backups/infisical-ready-$(date +%Y%m%d-%H%M%S).tar.gz" >/dev/null
venv/bin/python -m sbx_control.secret_store migrate
# sbx-control now requires sbx-infisical; host.py writes both and starts them.
venv/bin/python -m sbx_control.host install sbx-infisical.service sbx-control.service
echo "ok: Infisical headless on 127.0.0.1:$port"
