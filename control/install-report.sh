#!/bin/sh
# install.sh's last step, from the installed release: while the first-run
# setup is open, its last line is the onboarding link (a one-time token in
# the fragment), after a line that says when the browser will warn; else the
# server's address and what to do next, never a link. An installation whose
# settings are complete counts as set up (sbx-admin setup-infer: exit 0 set
# up, 3 still open).
#   SBX_PASSWORD=... install-report.sh   ("EMAIL: PASSWORD" lines install.sh generated, shown once)
#   SBX_ROLLBACK=... install-report.sh   (an upgrade's rollback script, host-install.sh)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

# The admins' generated passwords: each person has their own.
password() {
  [ -z "${SBX_PASSWORD:-}" ] && return
  echo "Sign in with (change it under CLI & access):"
  printf '%s\n' "$SBX_PASSWORD" | sed 's/^/  /'
  echo "  Add people under Settings → People (or sbx-admin user add EMAIL); each sets their own password."
}

status=0
sbx-admin setup-infer >/dev/null || status=$?
case "$status" in
  0) ;;
  3)
    # One link with a new one-time token (the one before stops working); its
    # last line is the link, on this terminal only.
    found=$(cd "$here" && venv/bin/python -m sbx_control.setup onboarding) || die "could not make the onboarding link (above)"
    link="" trusted="" domain=""
    eval "$found"
    [ -n "$link" ] || die "could not make the onboarding link"
    say "Installed: the rest of the setup is in your browser"
    echo "  Create your admin account, then choose the sandbox runtime and the coding agents."
    password
    echo "  Lost the link? sudo sbx-admin setup-link"
    echo
    if [ -z "$trusted" ] && [ -n "$domain" ]; then
      echo "Your browser will warn once: the certificate is self-signed until $domain's certificate is issued."
    elif [ -z "$trusted" ]; then
      echo "Your browser will warn once: the certificate is self-signed until the domain is set."
    fi
    echo "==> Open the onboarding: $link"
    exit 0 ;;
  *) die "could not read installation setup state" ;;
esac

eval "$(cd "$here" && venv/bin/python -m sbx_control.settings shell | sed 's/^/setting_/')" \
  || die "could not read the settings"
host=$setting_login
say "Done: https://$host"
password
# A runtime on this host gives a check that its sandboxes can't reach the host.
netcheck=""
[ ! -x /usr/local/sbin/sbx-netcheck ] || netcheck="
  - Check sandboxes can't reach this host: sbx-netcheck"
[ -z "${SBX_ROLLBACK:-}" ] || echo "Upgraded; the state before it is backed up. Roll back: sudo sh $SBX_ROLLBACK"
cat <<NEXT

Next:
  - On your machine: curl -fsSL https://$host/install | sh   (the sbx CLI), then sbx login
  - Try it on a public repo: in its checkout, sbx up try-1
  - Settings: sbx-admin config get [SECTION] | set SECTION JSON (the repos to serve, admins, network)
  - Agent connectors (off until connected): sbx-admin connect claude environment_secret=- (the secret on stdin)
  - Backups: sbx-admin backup; the recovery key: sbx-admin key show (keep it apart)$netcheck
NEXT
