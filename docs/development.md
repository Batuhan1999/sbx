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
| `web/` | The web app; it builds into `control/sbx_control/web_dist`, which is not committed. |
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
npm --prefix web test
npm --prefix web run build   # before the tests: the release test lays it out
PYTHONPATH=control .venv/bin/python -m unittest discover -s control/tests -q
.venv/bin/python -m unittest discover -s embed/tests -q
```

Every test class gets a throwaway installation of its own (settings, state
database and runtime directory under a temporary directory; see
`control/tests/support.py`), so the whole suite runs in one process. Checks
that need Linux process handling skip on macOS; CI
(`.github/workflows/check.yml`, the `check` job) runs everything on Ubuntu for
every pull request and every push to `main`, and also builds the web app and
checks shell syntax and release packaging. The desktop CLI's tests are in
`control/tests/test_cli.py`.

Unit tests do not prove a fresh installation, runtime recovery or a working app.
For lifecycle and service changes, run the
[acceptance checklist](agent-install.md#acceptance-checklist) on a test server.

## Web app

Edit `web/src`. `npm --prefix web run build` writes the bundle to
`control/sbx_control/web_dist`, which git ignores: the release workflow builds
it into each release archive, so a server installed from a release needs no
Node. A checkout needs that build before `install.sh` or `control/release.sh`,
which refuse without it, and before sbx-control serves the web app.
`npm --prefix web run dev` proxies the API and sign-in routes to a control
service on port 8080. Form conventions are in [web/README.md](../web/README.md).

## Pull requests

`main` changes only through pull requests. Its ruleset
([`.github/rulesets/main.json`](../.github/rulesets/main.json)) requires the
`check` job to pass and open review threads to be resolved, squash-merges each
PR into one commit, keeps the history linear and refuses direct and force
pushes; an admin may still merge a PR whose checks fail. No approval is
required while one maintainer merges, since GitHub does not let you approve
your own PR; once others have write access, set
`required_approving_review_count` to 1. Head branches are deleted on merge.

- One change per PR, small enough to review and pass CI on its own. Split
  larger work into several PRs; one may build on another by targeting its
  branch, and merges after it.
- The PR title becomes the commit subject on `main` and the change's line in
  the release notes. Write it like `main`'s history: what sbx does now, in the
  imperative, with no prefix. The squash commit's body is the PR's commit
  messages.
- Fill in the template. Label a PR `breaking` when an installation must act on
  upgrading past it, or the API, CLI, settings or recipe format change
  incompatibly; `no-release-notes` leaves it out of the notes.

CodeRabbit reviews each PR ([`.coderabbit.yaml`](../.coderabbit.yaml); it
reads [AGENTS](../AGENTS.md) and [CONTRIBUTING](../CONTRIBUTING.md)). Its
comments are advice, but each one is a review thread that must be resolved
before the PR merges: fix it or answer it, then resolve the thread
(`@coderabbitai resolve` resolves all of its threads). Until the repository
has 10 stars, CodeRabbit's open-source plan reviews only on request: comment
`@coderabbitai review`.

CI runs the full check on every PR, whatever its base; a new push to a PR
cancels its older run. Dependabot opens one PR a month that updates the pinned
GitHub Actions (`.github/dependabot.yml`).

## Releases

sbx is versioned with [SemVer](https://semver.org) tags on `main`:
`vMAJOR.MINOR.PATCH`. A PATCH release fixes things, a MINOR one adds things,
and a MAJOR one asks something of an installation that upgrades, or changes
the API, CLI, settings or recipe format incompatibly. Before 1.0 a MINOR
release may do that too; its notes then list those PRs under Breaking changes.

To release, tag a commit on `main` whose `check` passed, with an annotated tag,
and push the tag:

```sh
git fetch origin && git tag -a v0.2.0 origin/main -m "sbx v0.2.0"
git push origin v0.2.0
```

`.github/workflows/release.yml` runs the checks on the tag, builds the web
app, then `sbx-v0.2.0.tar.gz` (the source tree with a `VERSION` file and the
web app's bundle) and its `.sha256`, lays a release out from the unpacked
archive with `control/release.sh` to check it, and publishes the GitHub
release. Its notes list the titles of the
PRs merged since the previous release (grouped by
[`.github/release.yml`](../.github/release.yml)). When an upgrade needs steps,
write them in the tag's message below its first line (a second `-m`, or
`git tag -a` without `-m` for an editor); they go above the list. A tag with a
suffix (`v0.2.0-rc.1`) publishes a pre-release to try on a test installation
(one with `updates.prereleases` offers it in the web app); the next full
release's notes still start from the release before it.
Running the workflow by hand (**Actions → Release → Run workflow**) with a
version builds and checks the archive of any branch and publishes nothing. A
published tag never moves: fix a bad release with the next patch.

An installation [pins a release](reference.md#operating-the-host) by
installing from that release's archive (or a checkout of its tag, with the web
app built); `install.sh` names its release after the archive's `VERSION`, or
the checkout's nearest tag (`v0.2.0`, or `v0.2.0-3-g<commit>` for `main` past
it), and the web app shows it.

### Release layout

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

## Sandbox runtime

The runtime is the compute under sbx: it makes, lists, pauses and deletes VMs,
runs commands and writes files in them, builds their templates, and is the
last hop of a preview. Like the secret store, it is an installation's choice,
not an extension. Today it is E2B Embed on the host.

- `control/sbx_control/runtime/base.py` is the interface: the `Runtime` and
  `Handle` protocols, plain results (`Info`, whose `state` is `running` or
  `paused`), the errors (`Failed`, `CommandFailed`, `Timeout`, `Busy`,
  `NoRoom`), `Capabilities` and `TemplateSpec`.
- `control/sbx_control/runtime/__init__.py` is the one way in. The core and
  the extensions call `runtime.create`, `runtime.connect`,
  `runtime.sandboxes`, and so on, never a runtime's SDK.
  `test_runtime.py` fails if any module outside `runtime/embed/` imports `e2b`.
- `control/sbx_control/runtime/embed/` is everything that knows Embed: the SDK
  calls (`__init__.py`), room for sandboxes (`capacity.py`), how it enforces
  `hosted_services.allowed_cidrs` and limits its egress proxy (`network.py`,
  which `host-guard.sh` runs as `python -m sbx_control.runtime guard`), its
  storage (`storage.py`,
  read by `embed/storage-gc.py` and `embed/compact-snapshot.py`), its project
  on the host (`host.py`), its settings card (`connection.py`), and the
  loopback preview (`proxy.py`).

The rules stay in the core. Those are what sandboxes may reach, preview names,
login and access, and when to pause or delete. A runtime only carries them
out: it enforces the network ranges, and its `ingress` and `upstream_host`
are where nginx sends a preview. It also names what it reads from
`/etc/sbx/runtime.env` (`credential_keys`, and the `secret_key` that store
enable seals) and what it needs from the host (`capabilities`: managed
servers need `private_networks`, Amp's project caches need `snapshots`).

Two settings effects are the runtime's. `runtime-env` follows a domain
change: `sbx-apply runtime-env` calls `apply_host`, and Embed writes the
domain into its `.env`. `runtime-network` follows a change to what sandboxes
reach or to an extension's runtime layer. It has no path unit and stays
pending until `sbx-admin network apply` calls `apply_network`.

A second runtime implements the `Runtime` protocol in its own package, and
`runtime.current()` chooses it. One place still assumes Embed: `install.sh`
installs it.

### Embed's configuration

`vendor/e2b-embed/UPSTREAM` records the vendored runtime revision; the files
there stay unmodified ([notices](../THIRD_PARTY_NOTICES.md)). sbx's Compose
settings belong in `embed/compose.override.yaml`. An extension adds compose
files of its own with an `EmbedLayer` (`runtime/embed/host.py`), such as the
Amp connector's `connectors/amp/checkpoints/compose.yaml`. `/opt/e2b/.env`
has one writer, `write_embed_env` in `runtime/embed/host.py`: the keys sbx
owns come from the settings and the extensions, and the rest stays as Embed
wrote it. Render the combined configuration without a running daemon:

```sh
docker compose --env-file vendor/e2b-embed/compose/.env \
  -f vendor/e2b-embed/compose/compose.yaml \
  -f embed/compose.override.yaml config --quiet
```

Upgrading the runtime can recreate its containers, which existing sandboxes do
not survive. Configure private service networks and extensions' runtime layers
before creating sandboxes (`sbx-admin network apply` refuses once they exist).
An sbx upgrade asks first: `install.sh` runs `python -m sbx_control.runtime.embed
upgrade-check`, which lays the new release's compose files and `.env` keys over
a copy of `/opt/e2b` (`recreates` in `runtime/embed/host.py`), compares
`docker compose config --hash` with the running containers' labels, and
refuses while sandboxes exist when any of them would be recreated.

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
