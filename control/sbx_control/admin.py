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
  sbx-admin setup-reset [--sign-in]  back to 'being set up': the onboarding page opens again, and its address is printed
                                     (keeps the domain and connections; --sign-in clears the sign-in and admins)
  sbx-admin jobs [run ID]            what runs on its own (timers, the keeper, builds), and start one now
  sbx-admin token create EMAIL --label L   an API token for EMAIL, printed once (Linux, CI: no sbx login)
  sbx-admin token ls [EMAIL]         API tokens: id, email, label, when made and last used; never values
  sbx-admin token revoke ID          revoke one by the id token ls shows
  sbx-admin recipe rm OWNER/REPO     remove the recipe `sbx recipe apply` installed (refused while it has sandboxes)
  sbx-admin template rm KEY          delete a template (OWNER/REPO, default, default:<size>, sbx-base) and its build record
  sbx-admin template prune           delete the templates nothing starts from any more (the scheduler does it too)
  sbx-admin runtime list             the enabled sandbox runtimes: kind, default, capabilities, limits, state
  sbx-admin runtime verify [KIND]    take a throwaway sandbox through its life on one (default: the default)
  sbx-admin runtime apply            after editing /etc/sbx/runtime.env: check it, regenerate nginx, restart sbx-control
  sbx-admin runtime add KIND         enable a provider's runtime (e2b-cloud) with its API key, read from stdin
  sbx-admin runtime remove KIND      disable one (refused while it has sandboxes)
  sbx-admin runtime default KIND     the runtime new sandboxes run on unless asked otherwise
  sbx-admin runtime apply-pending    the change the web app queued (sbx-apply runtime-change runs it)
  sbx-admin preview NAME [--port N] [--listen N] [--preserve-host]  serve an app on host loopback for SSH forwarding
  sbx-admin server up NAME --kind postgres|clickhouse | --package PACKAGE
                                    run a private shared development server as a connection (sbx servers does
                                    the same over the API)
  sbx-admin server check NAME                   verify populated Postgres clones share physical blocks
  sbx-admin server exec NAME [--database DB] < seed.sql  import or prepare development base data
  sbx-admin server down NAME [--delete-data]     stop a managed server; optionally remove its owned data
  sbx-admin network apply           apply private service access before creating sandboxes
  sbx-admin endpoint ls|add|rm|grant|revoke   private services for sandboxes on provider runtimes (as sbx
                                     endpoints): add --server NAME | --name N --target HOST:PORT [--repo R];
                                     rm NAME; grant|revoke NAME REPO
  sbx-admin store enable|status     initialize and move to the default Infisical store, or inspect delivery status
  sbx-admin sign-in password         password sign-in (a first install's): each admin without a password
                                     gets a generated one, printed once
  sbx-admin user ls                  the people: email, admin, password set, last sign-in, API tokens
  sbx-admin user add EMAIL [--admin] add a person; prints a one-time link to set their password
  sbx-admin user link EMAIL          a new set-password link (a forgotten password); the old one stops working
  sbx-admin user admin EMAIL on|off  make them an admin or not
  sbx-admin user rm EMAIL            remove them: their sessions, API tokens and admin role end
  sbx-admin connect KIND k=v ...     connect one (github, claude, sign_in, domain, store); a secret
                                     given as k=- is read from stdin
  sbx-admin upgrade [TAG]            the releases newer than this one and their notes; with TAG
                                     (the newest), install it as the web app's Update does
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
    from . import extensions, hosted_services, reach  # noqa: F401  (their settings checks)
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
    from . import connections, extensions, platform
    optional = {c.kind for c in extensions.connectors()}
    bad = 0
    try:
        # Each runtime its keys as sbx-control hands them over at start: unsealed from runtime.env
        # (vault.runtime), so a runtime whose key is a reference into the store reads as set.
        platform.connect()
    except SystemExit as error:   # a key missing, a kind no package provides: what to fix, then the rest
        print(f"Sandbox runtime: not_connected · {error}")
        bad += 1
    for status in connections.status_all():
        print(f"{status.title}: {status.state} · {status.summary}")
        if status.kind in optional and status.state in ("off", "not_connected"):
            continue  # an extension left off or never set up is a choice, not a fault
        for check in connections.get(status.kind).test():
            print(f"  {'ok ' if check.ok else 'NO '} {check.name}: {check.detail}" + (f" — {check.fix}" if not check.ok and check.fix else ""))
            if not check.ok and check.help_url:
                print(f"      how to: {check.help_url}")
            bad += not check.ok
    from . import endpoints
    found = endpoints.describe()
    if found["endpoints"]:
        print(f"Endpoints: {len(found['endpoints'])} exposed to sandboxes on provider runtimes")
        for line in endpoints.doctor_lines(found):
            print(line)
        bad += sum(e["state"] == "failed" for e in found["endpoints"])
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
            if not check.ok and check.help_url:
                print(f"      how to: {check.help_url}", file=sys.stderr)
        return 1
    print(f"{status.title}: {status.state} · {status.summary}")
    return 0


def do_user(a) -> int:
    from . import users
    if a.action == "ls":
        for u in users.ls():
            print(f"{u['email']}\t{'admin' if u['admin'] else 'member'}\t"
                  f"{'password' if u['password'] else 'no password'}\t"
                  f"last sign-in {u['last_sign_in_at'] or 'never'}\t{u['tokens']} token(s)")
        return 0
    if not a.email or (a.action == "admin") != bool(a.value):
        print("user add|link|rm EMAIL, user admin EMAIL on|off", file=sys.stderr)
        return 2
    try:
        if a.action == "add":
            users.add(a.email, admin=a.admin, by="sbx-admin")
        if a.action in ("add", "link"):
            if a.action == "link" and not users.get(a.email) and a.email.lower() not in settings.current().admins:
                raise users.Refused(f"{a.email} is not here: sbx-admin user add {a.email}")
            print(f"set-password link for {a.email.lower()}, good for {users.LINK_DAYS} days and once:", file=sys.stderr)
            print(users.link_url(users.mint_link(a.email, by="sbx-admin")))
        elif a.action == "admin":
            users.set_admin(users.ensure(users.valid_email(a.email), by="sbx-admin")["email"], a.value == "on", by="sbx-admin")
        elif a.action == "rm":
            if not users.get(a.email) and a.email.lower() not in settings.current().admins:
                raise users.Refused(f"{a.email} is not here")
            users.remove(a.email, by="sbx-admin")
    except users.Refused as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
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


def do_runtime_change(a) -> int:
    """runtime add | remove | default: committed as a change from the web app
    is (runtime_changes.commit), under the lock it takes, so neither undoes
    the other; refused while one from the web app is queued (it would run
    after). add reads the runtime's key from stdin."""
    from . import host_requests, runtime, runtime_changes
    if not a.kind:
        print(f"runtime {a.action} KIND", file=sys.stderr)
        return 2
    known = a.action == "add" and a.kind in runtime.kinds()   # an unknown kind: add says so
    values = {runtime.make(a.kind).secret_key: sys.stdin.read().strip()} if known else {}
    try:
        with host_requests.lock(runtime_changes.KEY):
            if host_requests.requested(runtime_changes.KEY):
                raise runtime_changes.Busy("a runtime change from the web app is queued: apply it first "
                                           "(sbx-admin runtime apply-pending), then try again")
            runtime_changes.commit(a.action, a.kind, values)
    except (Exception, SystemExit) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    return 0


def do_runtime(a) -> int:
    from . import platform, runtime, runtime_changes, runtimes
    if a.action == "apply-pending":
        return runtime_changes.apply_pending()
    # Nothing connected first: add mends a runtime's missing key, and remove takes one whose keys are
    # missing (as from the web app); the change itself is checked as sbx-control will read it.
    if a.action in ("add", "remove"):
        return do_runtime_change(a)
    platform.connect()   # an unknown kind, a default not enabled or a runtime's missing keys stop here
    if a.action == "default":
        return do_runtime_change(a)
    if a.action == "apply":
        kinds, first = runtime.enabled(), runtime.default()   # checked before anything is asked of the host
        # nginx's upstreams are per enabled runtime (tls.sh), and sbx-control configures each runtime at start.
        settings.request_effects(settings.HostUnit.TLS, settings.HostUnit.CONTROL)
        print(f"runtimes {', '.join(kinds) or 'none'}{f' (default {first})' if first else ''}: nginx is "
              "regenerated and sbx-control restarts within seconds")
        return 0
    if a.action == "verify":
        if a.kind and a.kind not in runtime.enabled():
            print(f"no runtime {a.kind} is enabled here; the enabled ones are {', '.join(runtime.enabled())}",
                  file=sys.stderr)
            return 2
        return 0 if runtimes.verify(a.kind) else 1
    found = runtimes.describe()
    if a.json:
        print(json.dumps(found))
        return 0
    if not found["runtimes"]:
        print(runtime.NO_RUNTIME)
        return 0
    for r in found["runtimes"]:
        caps = ", ".join(name for name, on in r["capabilities"].items() if on) or "none"
        limits = ", ".join(f"{k}={v}" for k, v in r["limits"].items()) or "none known"
        print(f"{r['kind']:10} {'default' if r['default'] else '':8} {'this host' if r['host'] else 'hosted':10} "
              f"{r['status']['state']:14} {r['title']}: {r['status']['summary']}")
        print(f"{'':10} capabilities: {caps}; limits: {limits}")
    return 0


def do_upgrade(tag: str | None) -> int:
    from . import setup, updates
    if tag:
        return updates.follow(tag)
    s, installed = settings.current().updates, setup.version()
    try:
        found = updates.newer(updates.refresh(force=True), installed, s.prereleases)
    except updates.Unavailable as error:
        print(str(error), file=sys.stderr)
        return 1
    if updates.key(installed) is None:
        print(f"this installation runs {installed}, not a release: install one by hand "
              "(docs/reference.md, Operating the host)")
        return 0
    if not found:
        print(f"{installed} is the newest release of {s.repository}")
        return 0
    for release in found:
        print(f"== {release.tag}{' (breaking changes)' if release.breaking else ''}  {release.url}\n"
              f"{release.notes.strip()}\n")
    print(f"installed: {installed}. Read the notes above, then: sbx-admin upgrade {found[0].tag}")
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
    sub.add_parser("setup-infer")
    sub.add_parser("setup-reset").add_argument("--sign-in", action="store_true")
    jobs = sub.add_parser("jobs"); jobs.add_argument("verb", nargs="?", choices=["run"]); jobs.add_argument("id", nargs="?")
    user = sub.add_parser("user")
    user.add_argument("action", choices=["ls", "add", "link", "admin", "rm"])
    user.add_argument("email", nargs="?")
    user.add_argument("value", nargs="?", choices=["on", "off"], help="admin: on or off")
    user.add_argument("--admin", action="store_true", help="add: as an admin")
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
    runtime_cmd = sub.add_parser("runtime", help="the enabled sandbox runtimes")
    runtime_cmd.add_argument("action", choices=["list", "verify", "apply", "add", "remove", "default",
                                                "apply-pending"])
    runtime_cmd.add_argument("kind", nargs="?", help="verify: the runtime to verify (default: the default); "
                             "add, remove, default: the runtime")
    runtime_cmd.add_argument("--json", action="store_true", help="list: as GET /api/runtimes answers")
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
    server_up.add_argument("--kind", choices=["postgres", "clickhouse"])
    server_up.add_argument("--package", help="a connection package that runs its server (server: in its package.yaml)")
    server_up.add_argument("--secrets-stdin", action="store_true",
                           help="a new package server: NAME=VALUE lines on stdin for secrets it would otherwise make")
    server_up.add_argument('--data-root', type=pathlib.Path, help='existing XFS root; otherwise create an owned XFS image')
    server_up.add_argument('--storage-gb', type=int, help='new Postgres XFS image size (default: 32 GiB)')
    sub.add_parser("network").add_argument("action", choices=["apply"])
    endpoint = sub.add_parser("endpoint")
    endpoint.add_argument("action", choices=["ls", "add", "rm", "grant", "revoke"])
    endpoint.add_argument("name", nargs="?", help="rm, grant, revoke: the endpoint")
    endpoint.add_argument("repo", nargs="?", help="grant, revoke: owner/name")
    endpoint.add_argument("--repo", dest="repos", action="append", help="add: a repo that may use it (repeatable)")
    endpoint.add_argument("--server", help="add: a managed server's endpoint")
    endpoint.add_argument("--name", dest="endpoint_name", help="add: the endpoint's name")
    endpoint.add_argument("--target", help="add: HOST:PORT within network.internal_cidrs")
    sub.add_parser('store').add_argument('action', choices=['enable', 'status'])
    sub.add_parser("sign-in").add_argument("method", choices=["password"])
    sub.add_parser("upgrade").add_argument("tag", nargs="?")
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
        from . import hosted_services, runtime
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
                    presets = None
                    if a.secrets_stdin:
                        lines = [line for line in sys.stdin.read().splitlines() if line.strip()]
                        if not all("=" in line for line in lines):
                            raise ValueError("--secrets-stdin: NAME=VALUE lines")
                        presets = dict(line.split("=", 1) for line in lines)
                    result = hosted_services.up(a.name, a.kind, package=a.package, data_root=a.data_root,
                                                storage_gb=a.storage_gb, presets=presets)
            else:
                result = runtime.apply_network()
            print(json.dumps(result))
            return 0
        except (ValueError, RuntimeError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 1
    if a.cmd == "endpoint":
        from . import endpoints
        try:
            if a.action == "ls":
                result = endpoints.describe()
            elif a.action == "add":
                result = endpoints.add(server=a.server, name=a.endpoint_name, target=a.target, repos=a.repos,
                                       by="sbx-admin")
            elif a.action in ("grant", "revoke"):
                if not a.name or not a.repo:
                    p.error(f"endpoint {a.action} NAME REPO")
                result = (endpoints.grant if a.action == "grant" else endpoints.revoke)(a.name, a.repo)
            else:
                if not a.name:
                    p.error("endpoint rm NAME")
                result = endpoints.remove(a.name)
        except (ValueError, LookupError) as error:
            print(str(error).strip("'\""), file=sys.stderr)
            return 1
        print(json.dumps(result))
        return 0
    if a.cmd == "preview":
        from . import runtime
        if not 0 <= a.listen <= 65535 or (a.port is not None and not 1 <= a.port <= 65535):
            p.error("ports must be between 1 and 65535 (listen 0 chooses a free port)")
        try:
            runtime.preview(a.name, a.port, a.listen, preserve_host=a.preserve_host)
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
    if a.cmd == "user":
        return do_user(a)
    if a.cmd == "recipe":
        return do_recipe_rm(a.repo)
    if a.cmd == "template":
        return do_template(a)
    if a.cmd == "runtime":
        return do_runtime(a)
    if a.cmd == "jobs":
        from . import jobs
        if a.verb == "run":
            try:
                print(jobs.run_now(a.id or "", detached=True))
            except RuntimeError as error:
                print(str(error), file=sys.stderr)
                return 1
            return 0
        for j in jobs.jobs():
            when = f"last {j.get('last') or '-'}, next {j.get('next') or j.get('schedule') or '-'}" if j.get("available") else "not on this host"
            print(f"{j['group']:12} {j['title']:34} {when}{'  (run: sbx-admin jobs run ' + j['id'] + ')' if j.get('runnable') else ''}")
        return 0
    if a.cmd == "setup-reset":
        from . import setup
        setup.reset(sign_in=a.sign_in)
        print(setup.link())
        return 0
    if a.cmd == "setup-infer":  # exit 0: set up (marked now, or before); SETUP_OPEN: the wizard is still open
        from . import setup
        print("marked complete" if setup.infer_complete() else ("complete" if setup.complete() else "not configured yet"))
        return 0 if setup.complete() else SETUP_OPEN
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
    if a.cmd == "upgrade":
        return do_upgrade(a.tag)
    if a.cmd == "sign-in":
        from .connections import Refused, sign_in
        try:
            generated = sign_in.use_password(by="sbx-admin")
        except Refused as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1
        for email, password in generated.items():
            print(f"generated password for {email}: {password}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
