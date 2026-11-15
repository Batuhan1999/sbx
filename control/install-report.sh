#!/bin/sh
# install.sh's last step, from the installed release: the one-time setup link
# while the first-run setup is open, else the server's address and what to do
# next. An installation whose settings are complete counts as set up
# (sbx-admin setup-infer: exit 0 set up, 3 still open).
#   SBX_PASSWORD=... install-report.sh   (a password install.sh generated, shown once)
#   SBX_ROLLBACK=... install-report.sh   (an upgrade's rollback script, host-install.sh)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

# The shared password admits the admins (and allowed_emails), whoever holds it.
password() {
  [ -z "${SBX_PASSWORD:-}" ] && return
  echo "Sign in with an admin's email and the password: $SBX_PASSWORD"
  echo "  Whoever holds it can sign in as any admin. For a team, connect OIDC: sbx-admin connect sign_in method=oidc ..."
}

status=0
sbx-admin setup-infer >/dev/null || status=$?
case "$status" in
  0) ;;
  3)
    say "Done: finish the setup in your browser"
    echo "    $(sbx-admin setup-link)"
    echo "  Accept the temporary certificate, then choose IP access or a domain for trusted HTTPS."
    echo "  IP verification needs inbound TCP 80. Set your admin login, then connect your first repo."
    echo "  Lost the one-time link? sudo sbx-admin setup-link"
    password
    exit 0 ;;
  *) die "could not read installation setup state" ;;
esac

eval "$(cd "$here" && venv/bin/python -m sbx_control.settings shell | sed 's/^/setting_/')" \
  || die "could not read the settings"
host=$setting_login
say "Done: https://$host"
password
[ -z "${SBX_ROLLBACK:-}" ] || echo "Upgraded; the state before it is backed up. Roll back: sudo sh $SBX_ROLLBACK"
cat <<NEXT

Next:
  - On your machine: curl -fsSL https://$host/install | sh   (the sbx CLI), then sbx login
  - Try it on a public repo: in its checkout, sbx up try-1
  - Settings: sbx-admin config get [SECTION] | set SECTION JSON (the repos to serve, admins, network)
  - Agent connectors (off until connected): sbx-admin connect claude environment_secret=- (the secret on stdin)
  - Backups: sbx-admin backup; the recovery key: sbx-admin key show (keep it apart)
  - Check sandboxes can't reach this host: cd $here && venv/bin/python -m sbx_control.runtime.embed.netcheck
NEXT
