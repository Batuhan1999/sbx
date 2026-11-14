# Development

How to work on sbx itself. Contribution rules are in
[CONTRIBUTING](../CONTRIBUTING.md) and, for coding agents, [AGENTS](../AGENTS.md).

## Layout

| Path | What it is |
| --- | --- |
| `control/sbx_control/` | The control service (FastAPI): lifecycle, recipes, templates, previews, settings, `sbx-admin`. |
| `control/sbx_control/connectors/` | Built-in agent connectors ([extensions](extensions.md)). |
| `catalog/` | Shipped catalog packages and their shared helper `catalog/_lib/`. |
| `cli/sbx.py` | The desktop CLI ([macOS only](reference.md#limits)). |
| `web/` | The web app; its build is committed to `control/sbx_control/web_dist`. |
| `embed/` | sbx's configuration and optional builds for the E2B runtime. |
| `vendor/e2b-embed/` | The vendored runtime Compose stack; not edited. |
| `install.sh`, `control/*.sh` | Host installation, releases and activation. |
| `examples/` | Complete recipes for public applications. |

## Checks

The control service targets Python 3.12 on Ubuntu 24.04; the web app uses Node 22.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
npm --prefix web ci

.venv/bin/ruff check .
PYTHONPATH=control .venv/bin/python -m unittest discover -s control/tests -q
.venv/bin/python -m unittest discover -s embed/tests -q
npm --prefix web test
npm --prefix web run build
```

Every test class gets a throwaway installation of its own (settings, state
database and runtime directory under a temporary directory; see
`control/tests/support.py`), so the whole suite runs in one process. Checks
that need Linux process handling skip on macOS; CI
(`.github/workflows/check.yml`) runs everything on Ubuntu and also checks shell
syntax, release packaging and that the committed web bundle matches
`web/src`. The desktop CLI's tests are in `control/tests/test_cli.py`.

Unit tests do not prove a fresh installation, runtime recovery or a working app.
For lifecycle and service changes, run the
[acceptance checklist](agent-install.md#acceptance-checklist) on a test server.

## Web app

Edit `web/src`, never the generated bundle, and commit the rebuilt
`control/sbx_control/web_dist` with the change so a server needs no Node.
`npm --prefix web run dev` proxies the API and sign-in routes to a control
service on port 8080. Form conventions are in [web/README.md](../web/README.md).

## Releases

`sh control/release.sh DIR [VERSION]` lays out a release: the control service,
catalog, CLI, host scripts, the docs the setup brief carries and third-party
notices. On a host, releases live in `/opt/sbx-control/releases/<id>/` and
`/opt/sbx-control/current` points at the active one (`control/sbx_control/host.py`
describes the layout). `install.sh` stages a release there and installs it
together with the pinned E2B Compose stack; `host-install.sh` inside a staged
release activates it (switches `current` and rewrites every unit) without
reinstalling shared services, and an older release's `host-install.sh` rolls
back to it. When `sbx.db` exists, `host-install.sh` first stops what writes it
and backs it up with a `rollback.sh` (`python -m sbx_control.host
upgrade-backup`), then migrates. Review host changes without running the installer on your
workstation.

## Runtime configuration

`vendor/e2b-embed/UPSTREAM` records the vendored runtime revision; the files
there stay unmodified ([notices](../THIRD_PARTY_NOTICES.md)). sbx's Compose
settings belong in `embed/compose.override.yaml`; an extension adds compose
files of its own (`extensions.EmbedLayer`, such as the Amp connector's
`connectors/amp/checkpoints/compose.yaml`). `/opt/e2b/.env` has one writer,
`write_embed_env` in `control/sbx_control/host.py`: the keys sbx owns come
from the settings and the extensions, the rest stays as Embed wrote it.
Render the combined configuration without a running daemon:

```sh
docker compose --env-file vendor/e2b-embed/compose/.env \
  -f vendor/e2b-embed/compose/compose.yaml \
  -f embed/compose.override.yaml config --quiet
```

Upgrading the runtime can recreate its containers, which existing sandboxes do
not survive. Configure private service networks and extensions' runtime layers
before creating sandboxes (`sbx-admin network apply` refuses once they exist).

## Secret store backends

Where secret values live is an installation setting (`store.mode`, see
[secret storage](reference.md#secret-storage-and-live-updates)), not an
extension users pick. A backend implements the `SecretStore` protocol in
`control/sbx_control/secret_store/`: values by namespace and key (`get`, `put`,
`delete`, `list(namespace)`); sealed payloads whose reference an owner keeps in
its own column (`seal`, `unseal`, `discard`); and, for a store that can be
edited outside sbx, an optional `watch` that reports what changed. Each owner of
sealed values declares those columns in its table schema (`state.Schema.sealed`)
and deletes its rows with `secret_store.delete_rows`, which discards the
payloads first. An extension keeps its own secrets in a namespace of its own
(`vault.Key(name, namespace)`). Callers do not change when a backend is added.

## Secrets in development

Keep real credentials out of tests, examples, screenshots and command output; use
disposable values and resources. Backups contain readable metadata and operator
files as well as encrypted secrets, so keep whole archives private.
