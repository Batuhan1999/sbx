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
not an extension. An installation enables one or more runtimes, and each
sandbox runs on exactly one of them, chosen when it is created. Two are
built in: E2B Embed on the host, and E2B Cloud (E2B's hosted sandboxes).

- `control/sbx_control/runtime/base.py` is the interface: the `Runtime` and
  `Handle` protocols, plain results (`Info`, whose `state` is `running` or
  `paused` and whose `runtime` is its runtime's kind), the errors (`Failed`,
  `CommandFailed`, `Timeout`, `Busy`, `NoRoom`, `NotRunning`), `Capabilities`,
  `Limits` and `TemplateSpec`. The contract has two parts: every backend
  implements the compute part, and `HostSide` gives the host part (units,
  commands, the host's guard, disk, capacity, readiness) no-op defaults, so a
  hosted backend inherits it and Embed overrides it. Its docstring states the
  rule that a backend keeps no process-global configuration.
- `control/sbx_control/runtime/__init__.py` is the registry and the one way
  in. The core and the extensions call `runtime.create`, `runtime.connect`,
  `runtime.sandboxes`, and so on, never a runtime's SDK. `kinds()` lists the
  backends installed (the built-in `embed`, and any package's through the
  entry-point group `sbx.runtimes`, name = kind, value = `module:Class`),
  `enabled()` and `default()` read `SBX_RUNTIMES` and `SBX_RUNTIME_DEFAULT`
  from `/etc/sbx/runtime.env` (missing: `embed`; present and empty: none, and
  `get()` raises `NoRuntime`), `get(kind)` is one
  backend and `of(sandbox_id)` the one a sandbox runs on. Calls that name a
  sandbox route through `of()`; calls that make something (`create`,
  `build_template`, `templates`, `limits`, `capacity`, ...) take `kind`, where
  `None` is the default; `sandboxes()` lists every enabled runtime and tags
  each `Info` (only this installation's sandboxes: `create` adds the
  installation id, `state.installation_id()`, to the metadata as
  `sbx_install`; a runtime with `host = False` may be shared by several
  installations, so it is listed with that id as a metadata query, untagged
  sandboxes counting only when sbx's records know their id, and its
  templates and snapshots are named with the id's prefix, `template_name`;
  core code lists through the registry, never a backend's `sandboxes()`); the host's calls (`host_units`, `guard_host`, `apply_network`,
  `readiness`, `test`, ...) run over the enabled runtimes. `configure` hands
  each backend only its own `credential_keys`.
  `test_runtime.py` fails if any module outside `runtime/e2b/`,
  `runtime/embed/` and `runtime/e2b_cloud/` imports `e2b`, or any module
  outside `runtime/` imports a backend package.
- `control/sbx_control/runtime/e2b/` is the E2B SDK adapter the E2B-based
  backends share: `Compute`, the compute part of the contract over the SDK
  (create, connect, list, pause, snapshots, template builds and listings),
  its handles and errors as the runtime's (`Handle`, `call`: a sandbox that
  is not running as `NotRunning`), listings as `Info` (`as_info`) and a
  `TemplateSpec` as an E2B `Template` (`template`). A backend sets its
  connection once in `configure` (`connect_with(api_key=, api_url=,
  sandbox_url=, domain=)`, ConnectionConfig's options) and every SDK call
  passes it, so Embed and E2B Cloud run side by side with their own keys;
  nothing writes `E2B_*` into the process environment (`test_runtime.py`
  checks both).
- `control/sbx_control/runtime/e2b_cloud/` is E2B Cloud: `Compute` with
  `HostSide`'s no-op host part, its own keys (`E2B_CLOUD_*`), limits from
  them, an https `ingress` under the sandboxes' wildcard, and the traffic
  token (`upstream_token_header`, `upstream_token`): kept from create and
  connect, fetched once with connect after a restart.
- `control/sbx_control/runtime/fake/` is an in-memory backend for the test
  suite: `test_runtimes.py` runs an installation with Embed (mocked) and it
  side by side, and `sbx-admin runtime verify` passes against it.
- `control/sbx_control/runtimes.py` is the core's side of several runtimes:
  which one a new sandbox runs on (`choose`: the request's `runtime`, else the
  repo's setting from `PUT /api/repos/{repo}/runtime`, else the default,
  refused with the reason when it is not enabled, when managed servers are
  attached to the repo and it lacks `private_networks`, or when the recipe is
  beyond its limits), the runtimes a repo's sandboxes use (`used_by`, whose
  templates the scheduler builds), `GET /api/runtimes` and
  `/api/runtimes/available`, adding, changing and removing one (`add`,
  `remove`, `set_default`; `prepare` checks a change from the web app
  without writing), and `sbx-admin runtime verify [KIND]`.
- `control/sbx_control/runtime_changes.py` queues a change from the web app
  for the root side (`/run/sbx/apply/runtime-change`, a secret only as a
  store reference), applies it as root (`sbx-admin runtime apply-pending`,
  `perform`, shared with `sbx-admin runtime add | remove | default`) and
  records its outcome for `GET /api/runtimes/change`.

### Declaring a backend's settings

A backend that an admin configures in the web app declares its settings on
the class, each one of its `credential_keys`:

```python
from sbx_control.runtime import Preset, SettingField

class Provider(Compute, HostSide):
    credential_keys = ("PROVIDER_API_KEY", "PROVIDER_MAX_SANDBOXES")
    secret_key = "PROVIDER_API_KEY"
    settings = (
        SettingField("PROVIDER_API_KEY", "API key", secret=True, help="From the provider's dashboard."),
        SettingField("PROVIDER_MAX_SANDBOXES", "Sandboxes at once", kind="int", default="10",
                     limit="sandboxes"),
    )
    presets = (Preset("free", "Free", {"PROVIDER_MAX_SANDBOXES": "5"}, "https://provider.example/pricing"),)
```

`secret=True` is write-only: sealed into the managed store, shown as "set",
replaced only when a new value is typed. `kind="int"` takes a whole number,
at least 1; `kind="hostname"` a host name (lowercased); `limit` names the
`Limits` field it sets, which presets fill and which a refusal names;
`rekey=True` marks a setting that says where the secret is sent (a
provider's domain): changing it needs the secret typed again, so a stored
one never goes to a new place. A backend that declares nothing has its `secret_key`
as its one setting. A runtime on this host (`host = True`) declares none:
`install.sh` sets it up, and `can_run(facts)` says why this server can't run
it (`""` when it can) from `setup.host_facts()`.

When the provider refuses a request for a limit of its plan, the backend
raises `LimitRefused(message, field=..., asked=..., allowed=...)` for a
refusal it reads for sure (E2B Cloud's `refused_limit`: the timeout message
only; anything else passes on in the provider's words); the registry records it
(`runtime.limit_hit(kind)`, until the runtime's settings change), adds the
setting to change to the message, and raises it on. sbx never raises a
limit by itself.
- `control/sbx_control/runtime/embed/` is everything that knows Embed: its
  connection to the SDK adapter and its host side (`__init__.py`), room for sandboxes (`capacity.py`), how it enforces
  `hosted_services.allowed_cidrs` and limits its egress proxy (`network.py`,
  which `host-guard.sh` runs as `python -m sbx_control.runtime guard`), its
  storage (`storage.py`,
  read by `embed/storage-gc.py` and `embed/compact-snapshot.py`), its project
  on the host (`host.py`), its settings card (`connection.py`), and the
  loopback preview (`proxy.py`, `Runtime.preview`).

The rules stay in the core. Those are what sandboxes may reach, preview names,
login and access, and when to pause or delete. A runtime only carries them
out: it enforces the network ranges, and its `ingress` and `upstream_host`
are where nginx sends a preview. It also names what it reads from
`/etc/sbx/runtime.env` (`credential_keys`, and the `secret_key` that store
enable seals) and what it can do (`capabilities`): `private_networks` (its
sandboxes reach this host's private ranges) for `network.internal_cidrs` and
managed servers, `egress_rules` (it applies each sandbox's `deny_out`) for
`access.public_repos`, `snapshots` for Amp's project caches. Settings a
runtime can't enforce are refused by the capability's name and not offered
on the settings page (`reach.py`).

Templates are built per runtime (`template_builds`, keyed by runtime and
key): the base and default ones on every enabled runtime, a repo's own on the
runtimes its sandboxes use. Previews pick the sandbox's runtime: the gate
answers `X-Sbx-Runtime` beside `X-Sbx-Upstream`, and nginx (`tls.sh`,
`ip_previews.nginx`) has one `upstream` per enabled runtime and a `map` from
the kind to it (`ip_previews.runtime_upstreams`). An https ingress gets the
sandbox's own host as SNI (the Host nginx sends), verified against the
public CAs (`upstream_tls`). A runtime that answers a sandbox's ports only
with that sandbox's token declares `upstream_token_header`: the gate adds
`X-Sbx-Upstream-Token`, and nginx sends it in that header through a `map`
from the kind that is empty for every other runtime (`upstream_token`), so
another runtime never gets it and a browser's own value never passes. nginx
answers `/gate` itself with 404 on the public servers (`control.inc`).

Two settings effects are the runtime's. `runtime-env` follows a domain
change: `sbx-apply runtime-env` calls `apply_host`, and Embed writes the
domain into its `.env`. `runtime-network` follows a change to what sandboxes
reach or to an extension's runtime layer. It has no path unit and stays
pending until `sbx-admin network apply` calls `apply_network`.

`install.sh` enables Embed where the host can run it and E2B Cloud with
`E2B_CLOUD_API_KEY` in its environment; it writes `runtime.env` through
`python -m sbx_control.runtime env` (values from the environment, kinds
added to `SBX_RUNTIMES`, every other line kept). `sbx-admin runtime add`,
`remove` and `default` change it later (`runtimes.add`, `remove`,
`set_default`, through `vault.edit_runtime_file`).

### Adding a runtime

A runtime backend is a Python package that registers an entry point in the
group `sbx.runtimes`: the name is the runtime's kind, the value `module:Class`.

```toml
[project.entry-points."sbx.runtimes"]
my-provider = "my_package:MyProvider"
```

The built-in ones (`embed`: `sbx_control.runtime.embed:Embed`, `e2b-cloud`:
`sbx_control.runtime.e2b_cloud:Cloud`) are registered the same way in
`runtime/__init__.py` (`BUILTIN`). A backend over the E2B SDK builds on
`runtime/e2b/` (`Compute`) rather than calling the SDK itself. A kind that `SBX_RUNTIMES` enables but no
package provides fails at start with the kinds that exist.

The contract has two parts. Every backend implements the compute part of
`Runtime`: its sandboxes, their templates, `limits` and `capabilities`. The
host part (`host_units`, `guard_host`, `apply_host`, `apply_network`, `disk`,
`collect_storage`, `jobs`, ...) has no-op defaults in `runtime.base.HostSide`;
a backend with nothing on this host inherits it and implements nothing it does
not have. Embed overrides it.

- Read only your own keys from `/etc/sbx/runtime.env` and name them in
  `credential_keys`. Two backends never share a key name: a backend for a
  provider's E2B service reads its own key, never Embed's `E2B_API_KEY`.
- Keep no process-global configuration. Credentials go to each call, not
  into the process environment or module state where another backend reads
  them. `runtime/base.py` states the rule.
- Pass the test suite's runtime tests, which `runtime/fake/` (the in-memory
  backend the test suite uses) passes too, then
  `sbx-admin runtime verify <kind>` on a real installation: it creates a sandbox from the base template, runs a command,
  writes and reads a file, pauses and resumes it, reaches a preview port
  through the gate, and deletes it, reporting each step.

Previews follow each sandbox's runtime: the gate returns its kind
(`X-Sbx-Runtime`) next to `X-Sbx-Upstream`, and nginx has one `upstream` per
enabled runtime. A provider that gates its sandboxes' ports with a
per-sandbox token sets `upstream_token_header` and implements
`upstream_token(sandbox_id)`; nginx then sends the token to that runtime
only.

### Embed's configuration

`vendor/e2b-embed/UPSTREAM` records the vendored runtime revision; the files
there stay unmodified ([notices](../THIRD_PARTY_NOTICES.md)). sbx's Compose
settings belong in `embed/compose.override.yaml`. An extension adds compose
files of its own with a `RuntimeLayer` for the kind `embed`
(`Connector.runtime_layer`, applied in `runtime/embed/host.py`), such as the
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
