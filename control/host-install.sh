#!/bin/sh
# Install or activate the release this script sits in, as root. A release
# lives in /opt/sbx-control/releases/<id>/ (install.sh stages it there with
# control/release.sh). Safe to run again; an older release's host-install.sh
# rolls back to that release.
#
#   host-install.sh             install and activate
#   host-install.sh --prepare   only the venv (install.sh, before Embed starts)
#   host-install.sh --migrate   only the upgrade backup and the migration (install.sh, before Embed)
#
# On an upgrade (sbx.db exists) sbx-control and the connectors' services stop
# before sbx.db is migrated, after a backup of the state as it was into
# /var/lib/sbx/upgrade-backups/<time>/ (host.py upgrade_backup), which
# prints how to roll back: sh .../rollback.sh. They start again with this
# release. SBX_UPGRADE_BACKUP=done skips a backup this upgrade took already.
set -eu
release=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
releases=$(dirname "$release")
[ "$(basename "$releases")" = releases ] || { echo "run host-install.sh from <home>/releases/<id>, not $release" >&2; exit 1; }
cd "$release"

[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q -r requirements.txt
current=$(venv/bin/python -c 'from sbx_control import host; print(host.CURRENT)')
[ "$current" = "$(dirname "$releases")/current" ] || { echo "this release expects $current" >&2; exit 1; }
[ "${1:-}" != --prepare ] || exit 0

install -d -m 700 /etc/sbx
chmod -R go-rwx /etc/sbx
if [ "${SBX_UPGRADE_BACKUP:-}" != done ]; then
  venv/bin/python -m sbx_control.host upgrade-backup || { echo "no upgrade backup: nothing was changed (above)" >&2; exit 1; }
fi
venv/bin/python -m sbx_control.admin migrate || {
  echo "the migration of /var/lib/sbx/sbx.db failed (above); sbx-control and its connectors stay stopped." >&2
  echo "roll back with the newest /var/lib/sbx/upgrade-backups/*/rollback.sh" >&2; exit 1; }
[ "${1:-}" != --migrate ] || exit 0
venv/bin/python -m sbx_control.admin setup-infer >/dev/null || [ $? = 3 ]   # 3: the first-run setup is still open

# What /install hands to developers' machines: the sbx CLI, pointed at this server.
version=$(cat VERSION)
rm -rf dist && mkdir -p dist/stage
cp cli/sbx.py dist/stage/
echo "$version" > dist/stage/VERSION
(eval "$(venv/bin/python -m sbx_control.settings shell)" && echo "https://$login" > dist/stage/control)
tar -czf dist/sbx.tgz -C dist/stage . && rm -rf dist/stage
cp install.sh dist/install.sh && echo "$version" > dist/VERSION

# The switch: every unit and command runs $current.
ln -sfn "$release" "$current.new" && mv -Tf "$current.new" "$current"
cd "$current"
export HOME=/root   # the connectors' installers (a unit's prepare) need one
venv/bin/python -m sbx_control.host install
sh tls.sh > /tmp/sbx-tls.log 2>&1 || { tail -5 /tmp/sbx-tls.log; exit 1; }
venv/bin/python -m sbx_control.host prune
printf 'Activated %s\n' "$version"
venv/bin/python -m sbx_control.host list
