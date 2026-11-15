"""sbx-admin: the host's root CLI for the installation itself.

  sbx-admin migrate                  make and upgrade sbx.db's tables (the installer runs it)
  sbx-admin config get [SECTION]     the settings now (JSON)
  sbx-admin config set SECTION JSON  one section's values (repos: a JSON list)
  sbx-admin secret set NAME          a platform secret (NAME, or <extension>:NAME), read from stdin
  sbx-admin secret ls                the installation's secrets (platform, extensions', extra): facts, never values
  sbx-admin key show                 the recovery key (secret.key): store it apart from backups
  sbx-admin backup [--out FILE]      a bundle (default /var/lib/sbx/backups/sbx-<time>.tar.gz)
  sbx-admin restore FILE --key KEY   on a freshly installed host
  sbx-admin doctor                   every connection: its state and live checks
  sbx-admin catalog ls|connections|install DIR   the packages; the saved connections; an operator's package
  sbx-admin setup-link               the one-time link that opens the first-run setup (a new token each time)
  sbx-admin setup-reset [--sign-in]  back to 'being set up' (keeps the domain and connections; --sign-in clears the sign-in and admins)
  sbx-admin jobs [run ID]            what runs on its own (timers, the keeper, builds), and start one now
  sbx-admin token create EMAIL --label L   an API token for EMAIL, printed once (Linux, CI: no sbx login)
  sbx-admin token ls [EMAIL]         API tokens: id, email, label, when made and last used; never values
  sbx-admin token revoke ID          revoke one by the id token ls shows
  sbx-admin recipe rm OWNER/REPO     remove the recipe `sbx recipe apply` installed (refused while it has sandboxes)
  sbx-admin template rm KEY          delete a template (OWNER/REPO, default, default:<size>, sbx-base) and its build record
  sbx-admin template prune           delete the templates nothing starts from any more (the scheduler does it too)
  sbx-admin preview NAME [--port N] [--listen N] [--preserve-host]  serve an app on host loopback for SSH forwarding
  sbx-admin server up NAME --kind postgres|clickhouse  run a private shared development server as a connection
  sbx-admin server check NAME                   verify populated Postgres clones share physical blocks
  sbx-admin server exec NAME [--database DB] < seed.sql  import or prepare development base data
  sbx-admin server down NAME [--delete-data]     stop a managed server; optionally remove its owned data
  sbx-admin network apply           apply private service access before creating sandboxes
  sbx-admin store enable|status     initialize and move to the default Infisical store, or inspect delivery status
  sbx-admin sign-in password         password sign-in for the admins and allowed_emails (a first install's;
                                     one is generated and printed when none is set)
  sbx-admin connect KIND k=v ...     connect one (github, claude, sign_in, domain, store); a secret
                                     given as k=- is read from stdin
  sbx-admin CONNECTOR ...            a connector's own commands (amp)

What the host alone can do. Repositories, recipes, templates and sandboxes
go through the API, with the desktop CLI (`sbx recipe apply`, `sbx sandbox
...`, `sbx template build`) or any HTTP client with a token from
`sbx-admin token create` (docs/reference.md, API).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

from . import backup, delivery, paths, settings, state, vault

SETUP_OPEN = 3  # setup-infer's exit status while the first-run setup is open (install-report.sh, host-install.sh)


def do_config_set(section: str, text: str) -> int:
    from . import extensions
    try:
        value = json.loads(text)
        if section == "repos":
            s = settings.set_repos(list(value), by="sbx-admin")
        elif section in settings.SECTIONS:
            s = settings.update(section, {**getattr(settings.current(), section).model_dump(), **value}, by="sbx-admin")
        else:
            connector = extensions.get(section)
            if connector is None or not connector.section:
                print(f"no settings section {section}", file=sys.stderr)
                return 2
            s = settings.update(section, {**connector.settings().model_dump(), **value}, by="sbx-admin",
                                model=connector.section)
    except (ValueError, settings.Invalid) as error:
        print(f"not set: {error}", file=sys.stderr)
        return 1
    print(f"set: revision {s.revision}")
    return 0


def do_backup(out: pathlib.Path | None) -> int:
    if out is None:
        folder = paths.state_dir() / "backups"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        out = folder / time.strftime("sbx-%Y%m%d-%H%M%S.tar.gz", time.gmtime())
    manifest = backup.bundle(out)
    print(f"{out} ({out.stat().st_size // 1024} KB): {', '.join(manifest['parts'])}; key {manifest['key_fingerprint']}")
    print("the recovery key (`sbx-admin key show`) is not in the bundle: keep it apart")
    return 0


def do_restore(path: pathlib.Path, key: str) -> int:
    def unit(verb, name):
        subprocess.run(["systemctl", verb, name], check=False, capture_output=True)
    unit("stop", "sbx-control")
    try:
        manifest = backup.restore(path, key)
        state.migrate()
        backup.finish_restore()
    finally:
        unit("start", "sbx-control")
    settings.request_effects(settings.HostUnit.TLS)
    print(f"restored {manifest['domain']} from {manifest['created_at']}: point {manifest['domain']} and "
          f"*.{manifest['domain']} at this host; the certificate follows. Templates rebuild on the next deploy or "
          f"`sbx template build`.")
    return 0


def do_doctor() -> int:
    from . import connections, extensions
    optional = {c.kind for c in extensions.connectors()}
    bad = 0
    for status in connections.status_all():
        print(f"{status.title}: {status.state} · {status.summary}")
        if status.kind in optional and status.state in ("off", "not_connected"):
            continue  # an extension left off or never set up is a choice, not a fault
        for check in connections.get(status.kind).test():
            print(f"  {'ok ' if check.ok else 'NO '} {check.name}: {check.detail}" + (f" — {check.fix}" if not check.ok and check.fix else ""))
            bad += not check.ok
    return 1 if bad else 0


def do_connect(kind: str, pairs: list) -> int:
    from . import connections
    values = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep:
            print(f"{pair}: expected name=value", file=sys.stderr)
            return 2
        values[name] = sys.stdin.readline().rstrip("\n") if value == "-" else value
    try:
        connection = connections.get(kind)
    except KeyError:
        print(f"no connection {kind}", file=sys.stderr)
        return 2
    return _connected(lambda: connection.connect(values, by="sbx-admin"))


def _connected(connect) -> int:
    from . import connections
    try:
        status = connect()
    except connections.Refused as error:
        print(f"refused: {error}", file=sys.stderr)
        for check in error.checks:
            print(f"  {'ok ' if check.ok else 'NO '} {check.name}: {check.detail}", file=sys.stderr)
        return 1
    print(f"{status.title}: {status.state} · {status.summary}")
    if status.facts.get("generated_password"):
        print(f"generated password: {status.facts['generated_password']}")
    return 0


def do_token(a) -> int:
    from . import auth
    if a.action == "create":
        email = (a.email or "").strip().lower()
        if "@" not in email or not a.label:
            print("token create EMAIL --label LABEL", file=sys.stderr)
            return 2
        token = auth.mint_token(email, a.label)
        print(f"API token for {email} ({a.label}); shown once, keep it in a secret store:", file=sys.stderr)
        print(token)
        return 0
    if a.action == "revoke":
        try:
            gone = auth.revoke_token_id((a.email or "").strip().lower())
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 1
        print(f"revoked {gone['id']} ({gone['email']}, {gone['label']})")
        return 0
    for t in auth.tokens(a.email.lower() if a.email else None):
        print(f"{t['id']}  {t['email']:32} {t['label']:16} made {t['created_at']}  used {t['last_used_at'] or 'never'}")
    return 0


def do_recipe_rm(repo: str) -> int:
    from fastapi import HTTPException
    from . import platform, repo_onboarding, repo_path
    try:
        repo = repo_path.normalize(repo)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    platform.connect()
    try:
        print(json.dumps(repo_onboarding.remove_recipe(repo)))
    except HTTPException as error:
        print(error.detail, file=sys.stderr)
        return 1
    return 0


def do_template(a) -> int:
    from . import platform, template_builds
    platform.connect()
    try:
        if a.action == "prune":
            print(json.dumps({"deleted": template_builds.prune()}))
        else:
            if not a.key:
                print("template rm KEY", file=sys.stderr)
                return 2
            print(json.dumps(template_builds.remove(a.key.lower())))
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    from . import extensions
    connector = extensions.get(argv[0]) if argv else None
    if connector and connector.cli:
        return connector.cli(argv[1:])
    p = argparse.ArgumentParser(prog="sbx-admin", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for c in extensions.connectors():
        if c.cli:
            sub.add_parser(c.kind, add_help=False, help=f"{c.title}'s own commands")
    sub.add_parser("migrate")
    config = sub.add_parser("config").add_subparsers(dest="sub", required=True)
    config.add_parser("get").add_argument("section", nargs="?")
    config_set = config.add_parser("set")
    config_set.add_argument("section")
    config_set.add_argument("json")
    secret = sub.add_parser("secret").add_subparsers(dest="sub", required=True)
    secret.add_parser("set").add_argument("name")
    secret.add_parser("ls")
    sub.add_parser("key").add_subparsers(dest="sub", required=True).add_parser("show")
    sub.add_parser("backup").add_argument("--out", type=pathlib.Path)
    restore = sub.add_parser("restore")
    restore.add_argument("file", type=pathlib.Path)
    restore.add_argument("--key", required=True)
    sub.add_parser("doctor")
    catalog_cmd = sub.add_parser("catalog")
    catalog_cmd.add_argument("what", choices=["ls", "connections", "install"])
    catalog_cmd.add_argument("directory", nargs="?", type=pathlib.Path)
    sub.add_parser("setup-link")
    sub.add_parser("setup-infer")
    sub.add_parser("setup-reset").add_argument("--sign-in", action="store_true")
    jobs = sub.add_parser("jobs"); jobs.add_argument("verb", nargs="?", choices=["run"]); jobs.add_argument("id", nargs="?")
    token = sub.add_parser("token")
    token.add_argument("action", choices=["create", "ls", "revoke"])
    token.add_argument("email", nargs="?", help="create, ls: an email; revoke: the token's id")
    token.add_argument("--label", help="create: what the token is for (ci, laptop, ...)")
    recipe = sub.add_parser("recipe")
    recipe.add_argument("action", choices=["rm"])
    recipe.add_argument("repo")
    template = sub.add_parser("template")
    template.add_argument("action", choices=["rm", "prune"])
    template.add_argument("key", nargs="?")
    preview = sub.add_parser("preview", help="loopback-only app preview; forward its listener over SSH")
    preview.add_argument("name")
    preview.add_argument("--port", type=int)
    preview.add_argument("--listen", type=int, default=18096)
    preview.add_argument("--preserve-host", action="store_true",
                         help="keep an IP/localhost client Host for origin-bound apps; other hostnames are refused")
    server = sub.add_parser("server").add_subparsers(dest="action", required=True)
    server.add_parser("ls")
    server.add_parser('check').add_argument('name')
    server_exec = server.add_parser('exec')
    server_exec.add_argument('name')
    server_exec.add_argument('--database', help='Postgres database or ClickHouse database to run SQL in')
    server_down = server.add_parser('down')
    server_down.add_argument('name')
    server_down.add_argument('--delete-data', action='store_true')
    server_up = server.add_parser("up")
    server_up.add_argument("name")
    server_up.add_argument("--kind", choices=["postgres", "clickhouse"], required=True)
    server_up.add_argument('--data-root', type=pathlib.Path, help='existing XFS root; otherwise create an owned XFS image')
    server_up.add_argument('--storage-gb', type=int, help='new Postgres XFS image size (default: 32 GiB)')
    sub.add_parser("network").add_argument("action", choices=["apply"])
    sub.add_parser('store').add_argument('action', choices=['enable', 'status'])
    sub.add_parser("sign-in").add_argument("method", choices=["password"])
    connect = sub.add_parser("connect")
    connect.add_argument("kind")
    connect.add_argument("pairs", nargs="*")
    a = p.parse_args(argv)
    if a.cmd != "key":
        state.migrate()
    if a.cmd == "migrate":
        print("sbx.db is up to date")
        return 0
    if a.cmd == 'store':
        if a.action == 'enable':
            script = paths.shipped('infisical') / 'enable.sh'
            if not script.exists():
                p.error(f'no {script}: run sbx-admin on the host, from an installed release')
            admin = next(iter(settings.current().access.admins), 'admin@sbx.invalid')
            return subprocess.call(['sh', str(script), admin])
        s = settings.current().store
        print(json.dumps({'mode': s.mode, 'url': s.url, 'project': s.project,
                          'pending_sandboxes': sorted(delivery.waiting())}))
        return 0
    if a.cmd in ("server", "network"):
        from . import hosted_services, host_network
        try:
            if a.cmd == "server":
                if a.action == 'ls':
                    result = hosted_services.records()
                elif a.action == 'check':
                    result = hosted_services.check(a.name)
                elif a.action == 'exec':
                    return hosted_services.execute(a.name, a.database, sys.stdin)
                elif a.action == 'down':
                    result = hosted_services.down(a.name, delete_data=a.delete_data)
                else:
                    result = hosted_services.up(a.name, a.kind, data_root=a.data_root, storage_gb=a.storage_gb)
            else:
                result = host_network.apply()
            print(json.dumps(result))
            return 0
        except (ValueError, RuntimeError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 1
    if a.cmd == "preview":
        from .preview_proxy import serve
        if not 0 <= a.listen <= 65535 or (a.port is not None and not 1 <= a.port <= 65535):
            p.error("ports must be between 1 and 65535 (listen 0 chooses a free port)")
        try:
            serve(a.name, a.port, a.listen, preserve_host=a.preserve_host)
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    if a.cmd == "config" and a.sub == "get":
        from .settings.__main__ import main as settings_main
        return settings_main(["get"] + ([a.section] if a.section else []))
    if a.cmd == "config" and a.sub == "set":
        return do_config_set(a.section, a.json)
    if a.cmd == "secret" and a.sub == "set":
        value = sys.stdin.read().strip()
        if not value:
            print("the value comes on stdin", file=sys.stderr)
            return 2
        try:
            f = vault.put(vault.key(a.name), value, by="sbx-admin")
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        print(f"{f.name}: set, fingerprint {f.fingerprint}")
        return 0
    if a.cmd == "secret" and a.sub == "ls":
        platform = {k.row: k for k in vault.PLATFORM}
        for row in [*platform, *(r for r in vault.rows() if r not in platform)]:
            f = vault.facts(platform.get(row) or vault.key(row))
            print(f"{row:28} {'set · ' + f.fingerprint + ' · ' + str(f.updated_by) + ' · ' + str(f.updated_at) if f.set else 'not set'}")
        return 0
    if a.cmd == "key":
        print(paths.secret_key().read_text().strip())
        return 0
    if a.cmd == "backup":
        return do_backup(a.out)
    if a.cmd == "restore":
        return do_restore(a.file, a.key)
    if a.cmd == "doctor":
        return do_doctor()
    if a.cmd == "token":
        return do_token(a)
    if a.cmd == "recipe":
        return do_recipe_rm(a.repo)
    if a.cmd == "template":
        return do_template(a)
    if a.cmd == "jobs":
        from . import jobs
        if a.verb == "run":
            print(jobs.run_now(a.id or ""))
            return 0
        for j in jobs.jobs():
            when = f"last {j.get('last') or '-'}, next {j.get('next') or j.get('schedule') or '-'}" if j.get("available") else "not on this host"
            print(f"{j['group']:12} {j['title']:34} {when}{'  (run: sbx-admin jobs run ' + j['id'] + ')' if j.get('runnable') else ''}")
        return 0
    if a.cmd == "setup-reset":
        from . import setup
        print(setup.link(setup.reset(sign_in=a.sign_in)))
        return 0
    if a.cmd == "setup-infer":  # exit 0: set up (marked now, or before); SETUP_OPEN: the wizard is still open
        from . import setup
        print("marked complete" if setup.infer_complete() else ("complete" if setup.complete() else "not configured yet"))
        return 0 if setup.complete() else SETUP_OPEN
    if a.cmd == "setup-link":
        from . import setup
        if setup.complete():
            print("the setup is complete; connections are managed in Settings (or sbx-admin connect)")
            return 0
        print(setup.link(setup.mint_token()))
        return 0
    if a.cmd == "catalog":
        from . import catalog
        from .catalog import connections as saved
        if a.what == "install":
            if not a.directory:
                p.error("catalog install needs a package directory")
            try:
                name = a.directory.name
                version = catalog.save(name, catalog.folder_files(a.directory, name), "sbx-admin catalog install")
                kind = catalog.get(name).kind
            except (catalog.PackageError, OSError) as error:
                print(str(error), file=sys.stderr)
                return 1
            print(json.dumps({"name": name, "kind": kind, "installed": True, "version": version}))
            return 0
        if a.what == "ls":
            for package in catalog.listing():
                print(f"{package.name:24} {package.kind:12} {package.source:8} {package.title}")
            for problem in catalog.problems():
                print(f"not served: {problem}", file=sys.stderr)
        else:
            for c in saved.listing():
                print(f"{c['key']:12} {c['package']:20} {', '.join(f'{k}={v}' for k, v in c['inputs'].items())}  "
                      f"used by {', '.join(c['used_by']) or 'nobody'}")
        return 0
    if a.cmd == "connect":
        return do_connect(a.kind, a.pairs)
    if a.cmd == "sign-in":
        from .connections import sign_in
        return _connected(lambda: sign_in.use_password(by="sbx-admin"))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
