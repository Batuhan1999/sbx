"""The host's layout and every systemd unit the platform runs, from one list.

  /opt/sbx-control/releases/<id>/   a release (control/release.sh), with its own venv
  /opt/sbx-control/current          the active release (host-install.sh switches it)
  /usr/local/sbin/sbx-admin, sbx-apply, sbx-host-guard, and the runtime's
  (runtime.Runtime.host_commands)    commands into the active release

Every unit and command below runs the active release from CURRENT, so
switching releases is one symlink and a restart. Host scripts (tls.sh,
host-guard.sh, the connectors' own) run with CURRENT as their working
directory.

  python -m sbx_control.host install   write every unit and command, then (re)start them
  python -m sbx_control.host apply KEY a root-side effect sbx-control asked for (sbx-apply KEY)
  python -m sbx_control.host list      the units and whether each is wanted
  python -m sbx_control.host tools     install the host tools catalog packages declare
  python -m sbx_control.host upgrade-backup
                                       before an upgrade's migration (host-install.sh): stop
                                       what writes sbx.db, back up the state, write rollback.sh
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

HOME = pathlib.Path("/opt/sbx-control")
CURRENT = HOME / "current"
RELEASES = HOME / "releases"
KEEP_RELEASES = 3                       # the active one and two to roll back to
SBIN = pathlib.Path("/usr/local/sbin")
INFISICAL_COMPOSE = CURRENT / "infisical/compose.yaml"


def systemd_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("SBX_SYSTEMD_DIR") or "/etc/systemd/system")


@dataclass(frozen=True)
class Unit:
    """A unit file and what install does with it: restart (a service that
    must run this release), start (enable --now, leave a running one alone),
    enable (at boot only), or "" (started by a timer or path unit)."""
    name: str
    text: str
    action: str = "restart"
    wanted: bool = True                 # else disabled and stopped
    prepare: str = ""                   # shell run as root before it starts (a connector's install)
    ready: Callable[[], None] | None = None   # waits until it serves, before the next unit starts


def _service(description, service, *, after="network-online.target", install=True, unit_extra="") -> str:
    return (f"[Unit]\nDescription={description}\nAfter={after}\n{unit_extra}"
            f"[Service]\n{service.strip()}\n" + ("[Install]\nWantedBy=multi-user.target\n" if install else ""))


def _timer(description, timer) -> str:
    return f"[Unit]\nDescription={description}\n[Timer]\n{timer.strip()}\n[Install]\nWantedBy=timers.target\n"


def _python(module_args: str) -> str:
    return f"WorkingDirectory={CURRENT}\nExecStart={CURRENT}/venv/bin/python -m {module_args}"


def _apply_units(key: str) -> list[Unit]:
    """sbx-control asks for key by touching /run/sbx/apply/<key> (a settings
    change, settings.request_effects; an upgrade, updates.request); its path
    unit runs sbx-apply KEY as root."""
    from . import paths
    return [Unit(f"sbx-apply-{key}.path", f"[Unit]\nDescription=Apply the {key} effect sbx-control asked for\n"
                 f"[Path]\nPathExists={paths.run()}/apply/{key}\nUnit=sbx-apply-{key}.service\n"
                 "[Install]\nWantedBy=multi-user.target\n", action="start"),
            Unit(f"sbx-apply-{key}.service", _service(f"Apply the {key} effect sbx-control asked for",
                                                       f"Type=oneshot\nExecStart={SBIN}/sbx-apply {key}", install=False),
                 action="")]


def platform_effects() -> list:
    """The platform's effects that have a path unit (settings.HostUnit);
    extensions' keys re-render their services. runtime-network has none: it
    stays pending until `sbx-admin network apply`, which may refuse with
    sandboxes running."""
    from .settings import HostUnit
    return [unit.value for unit in HostUnit if unit != HostUnit.RUNTIME_NETWORK]


# Effects a release renamed, old: new. install carries a request still
# pending under the old name over (v0.1 named the runtime's after Embed).
RENAMED_EFFECTS = {"embed-env": "runtime-env", "embed-network": "runtime-network"}


def shell() -> dict:
    """The settings as the host scripts read them (settings.shell_lines)."""
    from . import extensions, settings
    lines = settings.shell_lines(settings.current(), {c.kind: c.settings() for c in extensions.connectors() if c.section})
    return dict(shlex.split(line)[0].split("=", 1) for line in lines)


def units() -> list[Unit]:
    """Every unit, in the order install starts them: the runtime's and the
    host's guard first, sbx-control before the connectors that call it."""
    from . import extensions, runtime, settings
    from .secret_store.infisical import env_file
    s = shell()
    infisical, port = env_file().exists(), settings.current().store.port
    runs = runtime.host_unit_names()
    needs = "".join(f"Requires={unit}\n" for unit in runs)
    found = [
        *runtime.host_units(),
        # Sandboxes' TCP leaves through the runtime's egress (host-guard.sh says what it allows).
        Unit("sbx-host-guard.service", _service(
            "Keep sandboxes off the host and the cloud metadata service; close the public interface",
            f"Type=oneshot\nRemainAfterExit=yes\nExecStart={SBIN}/sbx-host-guard",
            after=" ".join(runs) or "network-online.target", unit_extra=needs)),
        Unit("sbx-firewall.service", _service(
            "Keep sandboxes off the host's own ports",
            f"Type=oneshot\nRemainAfterExit=yes\nExecStart={SBIN}/sbx-firewall",
            after="docker.service network-online.target"), wanted=bool(s["sandbox_cidr"])),
        Unit("sbx-infisical.service", _service(
            "Infisical managed secret store",
            f"Type=oneshot\nRemainAfterExit=yes\nEnvironment=INFISICAL_ENV={env_file()} INFISICAL_PORT={port}\n"
            f"ExecStart=/usr/bin/docker compose -f {INFISICAL_COMPOSE} up -d --wait\n"
            "ExecStartPost=/usr/bin/curl --fail --silent --retry 60 --retry-connrefused --retry-all-errors "
            f"--retry-delay 2 --max-time 10 http://127.0.0.1:{port}/api/status\nTimeoutStartSec=10min",
            after="docker.service network-online.target", unit_extra="Requires=docker.service\n"),
             action="start", wanted=infisical),
        # The installation's backup bundle, whatever the secret store (backup.py).
        Unit("sbx-backup.service", _service(
            "Back up the installation", "Type=oneshot\n" + _python("sbx_control.backup scheduled"),
            after="sbx-infisical.service" if infisical else "network-online.target", install=False), action=""),
        Unit("sbx-backup.timer", _timer(
            f"Back up the installation every {s['backup_every_hours']} hours",
            f"OnBootSec=1h\nOnUnitActiveSec={s['backup_every_hours'] or 24}h\nPersistent=true"),
             action="start", wanted=bool(s["backup_every_hours"])),
        # Answers _acme-challenge.<domain> for the delegated certificate mode (acme_dns.py).
        Unit("sbx-acme-dns.service", _service(
            "Authoritative DNS for _acme-challenge.<domain> (sbx)",
            _python("sbx_control.acme_dns serve") + "\nRuntimeDirectory=sbx/acme\nRuntimeDirectoryPreserve=yes\n"
            "Restart=always\nRestartSec=5"), wanted=s["dns"] == "delegated" and bool(s["domain"])),
        *[unit for key in platform_effects() for unit in _apply_units(key)],
        Unit("sbx-control.service", _service(
            "sbx-control",
            f"WorkingDirectory={CURRENT}\nExecStart={CURRENT}/venv/bin/uvicorn sbx_control.app:app --host 127.0.0.1 "
            f"--port {_control_port()} --proxy-headers --forwarded-allow-ips 127.0.0.1\n"
            "StateDirectory=sbx\nRuntimeDirectory=sbx\nRuntimeDirectoryPreserve=yes\nRestart=always",
            after="network-online.target" + (" sbx-infisical.service" if infisical else ""),
            unit_extra="Requires=sbx-infisical.service\n" if infisical else ""), ready=wait_ready),
    ]
    for service in extensions.host_services():
        found.append(Unit(f"{service.name}.service", _service(service.description, service.service,
                                                              after="network-online.target sbx-control.service"),
                          wanted=service.enabled, prepare=service.prepare))
        if service.apply:
            found += _apply_units(service.apply)
    return found


def _control_port() -> int:
    from . import platform
    return platform.CONTROL_PORT


def commands() -> dict[str, str]:
    """/usr/local/sbin's commands into the active release."""
    def into(command):
        return f"#!/bin/sh\nset -eu\ncd {CURRENT}\nexec {command}\n"
    from . import runtime
    s = shell()
    host_ports = " ".join(s["host_ports"].split())
    return {
        "sbx-admin": into('venv/bin/python -m sbx_control.admin "$@"'),
        "sbx-apply": into('venv/bin/python -m sbx_control.host apply "$@"'),
        "sbx-host-guard": into("sh host-guard.sh"),
        **runtime.host_commands(into),
        # Sandboxes may reach the host's nginx (the gate) and nothing else of it.
        "sbx-firewall": (
            "#!/bin/sh\nset -eu\n"
            "iptables -w -N SBX-HOST 2>/dev/null || iptables -w -F SBX-HOST\n"
            "iptables -w -A SBX-HOST -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN\n"
            "iptables -w -A SBX-HOST -p udp --dport 53 -j RETURN\n"
            f"for port in {host_ports}; do iptables -w -A SBX-HOST -p tcp --dport $port -j RETURN; done\n"
            "iptables -w -A SBX-HOST -j DROP\n"
            f"iptables -w -C INPUT -s {s['sandbox_cidr']} -j SBX-HOST 2>/dev/null || "
            f"iptables -w -I INPUT 1 -s {s['sandbox_cidr']} -j SBX-HOST\n"
            f"iptables -w -C FORWARD -s {s['sandbox_cidr']} -d 169.254.169.254 -j DROP 2>/dev/null ||\n"
            f"  iptables -w -I FORWARD 1 -s {s['sandbox_cidr']} -d 169.254.169.254 -j DROP\n")
        if s["sandbox_cidr"] else None,
    }


def _systemctl(*args, check=False):
    subprocess.run(["systemctl", *args], check=check)


def _activate(unit: Unit):
    if not unit.action:
        return
    if not unit.wanted:
        _systemctl("disable", "--now", "-q", unit.name)
        return
    if unit.prepare:
        subprocess.run(["sh", "-c", unit.prepare], check=True, cwd=CURRENT)
    if unit.action == "restart":
        _systemctl("enable", "-q", unit.name)
        _systemctl("restart", unit.name, check=True)
    elif unit.action == "start":
        _systemctl("enable", "-q", "--now", unit.name)
    else:
        _systemctl("enable", "-q", unit.name)
    if unit.ready:
        unit.ready()


def wait_ready(timeout: float = 120):
    """Until sbx-control answers: its lifespan (migrations, the platform's
    connection) has finished once uvicorn serves a request."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{_control_port()}/"
    while True:
        try:
            urllib.request.urlopen(url, timeout=5)
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit(f"sbx-control did not answer on {url}; journalctl -u sbx-control")
            time.sleep(0.5)


def write(found: list[Unit]):
    directory = systemd_dir()
    for name, text in commands().items():
        path = SBIN / name
        if text is None:
            path.unlink(missing_ok=True)
            continue
        path.write_text(text)
        path.chmod(0o755)
    for unit in found:
        (directory / unit.name).write_text(unit.text)
    _systemctl("daemon-reload")


def install(names: list[str] | None = None):
    from . import paths
    requests = paths.run() / "apply"
    requests.mkdir(parents=True, exist_ok=True)
    found = units()
    if not names:
        remove_stale_effects(found)
    # A request carried over to its new name, and the path unit that watches it.
    moved = set()
    for old, new in RENAMED_EFFECTS.items():
        if (requests / old).exists():
            (requests / old).replace(requests / new)
            moved.add(f"sbx-apply-{new}.path")
    write(found)
    for unit in found:
        if not names or unit.name in names or unit.name in moved:
            _activate(unit)


def remove_stale_effects(found: list[Unit]):
    """Disable and delete the settings effects' units (sbx-apply-*) that
    found doesn't have: an effect an upgrade renamed or dropped, which
    sbx-apply would refuse as unknown."""
    directory = systemd_dir()
    known = {unit.name for unit in found}
    stale = sorted(path.name for path in directory.glob("sbx-apply-*")
                   if path.suffix in (".path", ".service") and path.name not in known)
    if not stale:
        return
    _systemctl("disable", "--now", "-q", *stale)
    for name in stale:
        (directory / name).unlink()


def apply(key: str) -> int:
    """sbx-apply KEY, as root. The request goes first, so one made while
    this runs triggers it again; an upgrade's names its release (updates.py),
    a runtime change's holds the change (runtime_changes.py)."""
    from . import paths, runtime
    if key == "upgrade":
        from . import updates
        return updates.install_requested()
    if key == "runtime-change":   # sbx-admin runtime apply-pending: the request holds the change
        from . import runtime_changes
        return runtime_changes.apply_pending()
    (paths.run() / "apply" / key).unlink(missing_ok=True)
    if key == "tls":
        acme = next(u for u in units() if u.name == "sbx-acme-dns.service")
        write([acme])
        _activate(acme)
        return subprocess.call(["sh", "tls.sh"], cwd=CURRENT)
    if key == "endpoints":
        from . import endpoints
        return endpoints.apply_host()
    if key == "host-guard":
        _systemctl("restart", "sbx-host-guard", check=True)
    elif key == "control":
        _systemctl("restart", "sbx-control", check=True)
    elif runtime.apply_host(key):
        pass   # a runtime's own
    else:
        from . import extensions
        names = {f"{s.name}.service" for s in extensions.host_services() if s.apply == key}
        if not names:
            print(f"unknown effect {key}", file=sys.stderr)
            return 2
        found = [u for u in units() if u.name in names]
        write(found)
        for unit in found:
            _activate(unit)
    return 0



def install_tools():
    """The host tools the catalog's packages declare (host_tools: name ->
    install command), each installed unless it is on PATH already."""
    from . import catalog
    for name, command in sorted(catalog.host_tools().items()):
        if shutil.which(name) is None:
            print(f"installing {name} (catalog host tool)", flush=True)
            subprocess.run(["sh", "-c", command], check=True, stdout=subprocess.DEVNULL)


def writers() -> list[str]:
    """The running long-lived services that run the platform (sbx-control
    and the connectors' host services): what writes sbx.db. Read from the
    unit files, not the settings, so an older release's database is not
    read by this one before its migration; any unit that runs code from
    under HOME counts, whichever release directory it names."""
    found = []
    for unit in sorted(systemd_dir().glob("*.service")):
        text = unit.read_text()
        if (str(CURRENT.parent) in text and "\nRestart=" in text
                and subprocess.run(["systemctl", "is-active", "--quiet", unit.name]).returncode == 0):
            found.append(unit.name)
    return sorted(found, key=lambda name: (name != "sbx-control.service", name))


def upgrade_backup() -> pathlib.Path | None:
    """Before an upgrade migrates sbx.db (host-install.sh): stop its writers,
    then keep the state as it was in UPGRADE_BACKUPS/<time>/: a copy of
    sbx.db, the platform's unit files, the active release's link and a
    backup bundle made by that release (sbx-admin backup: the secret
    stores' data too), with rollback.sh to go back. None on a first install.
    The writers stay stopped; the install starts them again."""
    from . import paths
    if not paths.database().exists():
        return None
    stopped = writers()
    if stopped:
        _systemctl("stop", *stopped)
    out = paths.state_dir() / "upgrade-backups" / time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    try:
        out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        out.parent.chmod(0o700)
        for attempt in range(1, 100):
            try:
                out.mkdir(mode=0o700)
                break
            except FileExistsError:   # another upgrade within the second
                out = out.with_name(f"{out.name.partition('.')[0]}.{attempt}")
        _copy_database(paths.database(), out / "sbx.db")
        # The runtimes and their keys as the active release knows them: the
        # upgrade may enable one that release can't load (rollback.sh).
        if paths.runtime_env().exists():
            (out / "runtime.env").touch(mode=0o600)
            shutil.copy2(paths.runtime_env(), out / "runtime.env")
            (out / "runtime.env").chmod(0o600)
        units = out / "systemd"
        units.mkdir(mode=0o700)
        directory = systemd_dir()
        from . import runtime
        own = {unit.name for unit in runtime.host_units()} | set(stopped)
        for path in sorted(directory.iterdir()):
            if path.name.startswith("sbx-") or path.name.removesuffix(".d") in own:
                (shutil.copytree if path.is_dir() else shutil.copy2)(path, units / path.name)
        previous = CURRENT.resolve() if CURRENT.is_symlink() else None
        bundle = out / "bundle.tar.gz"
        # The release that made the state backs it up (sbx-admin is the active release's).
        made = subprocess.run([str(SBIN / "sbx-admin"), "backup", "--out", str(bundle)], capture_output=True, text=True)
        if made.returncode != 0:
            raise SystemExit(f"sbx-admin backup failed: {(made.stderr or made.stdout).strip()[-500:]}")
    except BaseException:
        if stopped:
            _systemctl("start", *stopped)   # nothing changed yet: the active release runs on
        print(f"the upgrade stopped before changing anything; partial backup in {out}", file=sys.stderr, flush=True)
        raise
    rollback = out / "rollback.sh"
    rollback.write_text(_rollback_script(out, previous, stopped, paths.database()))
    rollback.chmod(0o700)
    print(f"upgrade backup: {out} (sbx.db, unit files, bundle.tar.gz; stopped {' '.join(stopped)})", flush=True)
    print(f"rollback: sh {rollback}", flush=True)
    return out


def _copy_database(source: pathlib.Path, target: pathlib.Path):
    """A consistent copy of sbx.db, its write-ahead log included."""
    import sqlite3
    from contextlib import closing
    target.touch(mode=0o600)
    with closing(sqlite3.connect(source)) as conn, closing(sqlite3.connect(target)) as copy:
        conn.backup(copy)


def _rollback_script(out: pathlib.Path, previous: pathlib.Path | None, stopped: list, database: pathlib.Path) -> str:
    from . import paths
    units, q = " ".join(stopped) or "sbx-control.service", shlex.quote
    switch = (f"ln -sfn {q(str(previous))} {q(str(CURRENT))}.new && mv -Tf {q(str(CURRENT))}.new {q(str(CURRENT))}\n"
              if previous else "")
    # A release that watched its effects under the old names (its unit files
    # say) reads pending requests under those names.
    requests, saved = paths.run() / "apply", out / "systemd"
    older = " || ".join(f"[ -e {q(str(saved / f'sbx-apply-{old}.path'))} ]" for old in RENAMED_EFFECTS)
    moves = "".join(f"  [ ! -e {q(str(requests / new))} ] || mv -f {q(str(requests / new))} {q(str(requests / old))}\n"
                    for old, new in RENAMED_EFFECTS.items())
    renamed_back = f"if {older}; then\n{moves}fi\n"
    saved_env = out / "runtime.env"
    runtime_env = (f"install -m 600 {q(str(saved_env))} {q(str(paths.runtime_env()))}\n"
                   if saved_env.exists() else "")
    # nginx as that release writes it: its HTTPS server may own 443 itself, so
    # this release's stream front goes, and that release's tls.sh makes the
    # container afresh, in its own layout (an upgrade again makes it anew too).
    nginx = paths.tls() / "nginx"
    https = (f"rm -f {q(str(nginx))}/*.stream {q(str(nginx))}/endpoint-*-ca.pem\n"
             f"docker rm -f sbx-tls-nginx >/dev/null 2>&1 || true\n"
             f"(cd {q(str(previous))} && sh tls.sh) > /tmp/sbx-rollback-tls.log 2>&1 || "
             f"echo \"nginx: tls.sh failed (/tmp/sbx-rollback-tls.log); run it again: cd {previous} && sh tls.sh\" >&2\n"
             if previous else "")
    return (f"#!/bin/sh\n# Go back to the state before the upgrade backed up here: the release that was\n"
            f"# active, its unit files, sbx.db and runtime.env. Sandboxes and the runtime are left as they are.\n"
            f"set -eu\n"
            f"systemctl stop {units} || true\n{switch}"
            f"cp -a {q(str(out / 'systemd'))}/. {q(str(systemd_dir()))}/\n{renamed_back}"
            f"rm -f {q(str(database))}-wal {q(str(database))}-shm\n"
            f"install -m 600 {q(str(out / 'sbx.db'))} {q(str(database))}\n{runtime_env}"
            f"systemctl daemon-reload\n"
            # The upgrade may have disabled the release's effect paths (remove_stale_effects).
            f"for unit in {q(str(out / 'systemd'))}/sbx-apply-*.path; do\n"
            f"  [ ! -e \"$unit\" ] || systemctl enable -q --now \"$(basename \"$unit\")\" || true\ndone\n"
            f"systemctl start {units}\n{https}"
            f"echo 'rolled back to {previous or 'the release before'}'\n")


def prune_releases(keep: int = KEEP_RELEASES):
    active = CURRENT.resolve()
    found = sorted((p for p in RELEASES.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in found[keep:]:
        if path.resolve() != active:
            shutil.rmtree(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("install").add_argument("names", nargs="*")
    sub.add_parser("apply").add_argument("key")
    sub.add_parser("list")
    sub.add_parser("prune")
    sub.add_parser("tools")
    sub.add_parser("upgrade-backup")
    args = parser.parse_args(argv)
    if args.action == "tools":
        install_tools()
    elif args.action == "upgrade-backup":
        upgrade_backup()
    elif args.action == "list":
        for unit in units():
            print(f"{unit.name}\t{'wanted' if unit.wanted else 'off'}\t{unit.action or '-'}")
    elif args.action == "install":
        install(args.names)
    elif args.action == "prune":
        prune_releases()
    else:
        return apply(args.key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
