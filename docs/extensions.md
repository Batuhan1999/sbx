# Extensions

The core of sbx is the sandbox lifecycle (create, prepare, restart, pause,
resume, delete; see the [reference](reference.md#lifecycle)). Everything
specific to an agent product or a service provider is an extension:

| Kind | Where | Examples |
| --- | --- | --- |
| Agent connector | `control/sbx_control/connectors/<name>`, or an installed Python package | Claude Code, Codex, [Amp](amp-orbs.md) |
| Catalog package | `catalog/<name>/`, `/etc/sbx/catalog/<name>/`, or a repository's `.sbx/services/<name>/` | Postgres, ClickHouse, Neon, PlanetScale, Supabase, Tinybird, Upstash, AWS |
| GitHub | `control/sbx_control/connections/github.py`, a platform connection | `gh`, git credentials and PR previews |
| Sandbox runtime | `control/sbx_control/runtime/<kind>/`, or an installed Python package registered under `sbx.runtimes` | E2B Embed |

Where secret values are kept is not an extension but an installation setting,
chosen when sbx is installed ([secret storage](reference.md#secret-storage-and-live-updates)).

Sandbox runtimes, the compute under the lifecycle, are pluggable through
their own registry (`sbx_control.runtime`), not the extensions' one. An
installation runs the runtimes it enables ([runtimes](reference.md#runtimes)),
and each sandbox runs on one of them. Embed is built in; another runtime is a
package ([runtime backends](#runtime-backends)). Extensions reach the runtimes
only through `sbx_control.runtime`, never through a runtime's SDK
([sandbox runtime](development.md#sandbox-runtime)).

The core never names an extension. It asks the registry
(`control/sbx_control/extensions.py`) for contributions, keeper policies,
origins, reserved ports, developers' agent logins and how git reaches a
repository, and reaches the catalog through one interface,
`extensions.packages()` (`catalog/provisioning.py`: provision, refresh,
release, paused, reconcile, contributions). Extensions reach sandboxes only
through the public domain API.

## Contributions

Every extension that puts something into a sandbox returns one record,
`Contribution` (`control/sbx_control/contributions.py`):

| Field | Meaning |
| --- | --- |
| `name` | Unique name, for example `claude-code`, `github`, `aws-profile`. |
| `template` | A `TemplateLayer` built into templates once: `apt` packages, root-owned `files`, `root` and `user` commands (credential-free), and `artifacts` the host produces at build time and unpacks into the template. |
| `runtime` | A `RuntimeLayer` written into the sandbox's tmpfs on every runtime write (create, prepare, env change, cold boot): `env`, `files` (under `/run/sbx/ext/<name>/`), `sources` (files shells source), `dirs`, `git_config`, `links` (symlinks, such as an agent's login file on disk pointing into the tmpfs). Per-sandbox and secret values go here. The tmpfs is small (a fifth of the VM's memory) and also holds sbx's env and process supervisor's state: put an agent's growing files (sessions, caches, packages) on disk, as the Codex connector does with `CODEX_HOME` while its login stays in the tmpfs. |
| `scrub_paths` | Paths removed before a reusable snapshot of a sandbox is published. |
| `ready` | A command that exits 0 when the contribution is in place: run at the end of a template build (failing it) and when attaching to a sandbox (verifying only). |

A template is the platform layer, the template layers of every enabled
extension, and the repository's recipe. Their digest is the template's
fingerprint, so enabling a connector or changing a pinned version makes
templates stale and they are rebuilt ([templates](reference.md#templates)). An
extension never adds steps to the template builder itself.

An extension can instead put its layer into the base template of every
installation, whatever its settings, with `base_contribution()`: a
`Contribution` whose `template`, `ready` and `scrub_paths` are read (its
`runtime` is not). It is part of the base's fingerprint, so only a change to
the layer itself (a new pinned version) rebuilds the base and the templates on
it; connecting, enabling or disabling the extension rebuilds nothing. Amp does
this, so choosing Amp never waits for a build. Use it for what is large or
slow to build, at the cost of every template carrying it.

## Agent connectors

A connector lets an agent product start sessions in sandboxes: Claude Code's
self-hosted runner, Amp's orb provider. Subclass `extensions.Connector`:

```python
from sbx_control import extensions
from sbx_control.contributions import Check, Contribution, TemplateLayer

class MyAgent(extensions.Connector):
    kind = "my-agent"            # unique; also the sandboxes' metadata connector=<kind>
    title = "My Agent"

    # Settings UI (from connections.Connection): status(), test(values),
    # connect(values, by=...), disconnect(by=...)

    def contribution(self, target):          # target: repo, name ("" for a template), trusted, env
        return Contribution("my-agent",
            template=TemplateLayer(root=("npm install -g my-agent@1.2.3",)),
            ready=Check("my-agent --version"))
```

Every other hook is optional. These attributes are read on hot paths, so they
are plain values rather than part of the contribution:

| Attribute | Use |
| --- | --- |
| `ports` | Ports its processes listen on in a sandbox; never treated as detected previews. |
| `activity` | Directories of agent transcripts; a recent `*.jsonl` there counts as use for the keeper. |
| `busy` | `pgrep -f` patterns; a matching process counts as use. |
| `login` | `extensions.Login`: where a developer's own login for the agent goes in a sandbox, and how `sbx login <kind>` gets it (a pasted token kept in the Keychain, or the agent's own login file). The CLI reads these from `GET /api/me`; the server never sees the login. |
| `section` | Its settings: a `settings.ExtensionSection` model, stored under its kind; `settings()` reads it and `save_settings()` writes it, asking for the host effects the model's `applies` names. |
| `cli` | A function run as `sbx-admin <kind> ...`. |

| Method | Use |
| --- | --- |
| `base_contribution()` | Its layer in every installation's base template, whatever its settings ([contributions](#contributions)). |
| `migrate(conn)` | Create or update the connector's own tables in `sbx.db` (run by `state.migrate()`). |
| `on_restore(conn)` | Settle its own rows after a backup is restored on another host. |
| `router()` | A FastAPI `APIRouter` for its routes (for example job or order endpoints). |
| `workers()` | Functions run on threads from server start until the stop event is set. |
| `keeper_policy(info, use)` | For its own sandboxes: `Policy(keep_awake, decided, keep_paused)`. `use` holds the gate's last access, whether a preview resumed it, and a probe (SSH sessions, agent activity, busy processes) taken only when read. |
| `origin(info)` | How views label its sandboxes: `{kind, label, detail, href}`. |
| `host_services()` | systemd services it runs on the host (`HostService`), rendered by the installer. |
| `runtime_layer(kind)` | A `RuntimeLayer` (`runtime/base.py`) for the runtime of that kind, or `None`: keys it adds to the env of the services that run the runtime on this host and compose files merged over theirs. Each backend applies it its own way (Embed: `runtime/embed/host.py`). |
| `git_access(repo)` | For the extension through which the installation reaches repositories (GitHub): a context giving host-side git its URL and environment. |

Rules:

- Create, pause and delete sandboxes through the public domain API, and tag them
  with `metadata.connector=<kind>`. Keep session state in your own tables.
- Reach VMs only through `sbx_control.runtime` (`runtime.connect`,
  `runtime.sandboxes`, ...); `on_resume(sbx, info)` gets a `runtime.Handle`.
  Check `runtime.capabilities()` before relying on what not every runtime has,
  such as filesystem snapshots.
- Put the agent's binary in the template layer and per-sandbox credentials in the
  runtime layer; never write secrets into a template.
- Name transcript directories in `activity` and private paths in `scrub_paths`.

Built-in connectors are listed in `extensions.BUILTIN`. A third-party connector
is an installed Python package that registers an entry point:

```toml
[project.entry-points."sbx.connectors"]
my-agent = "my_package:MyAgent"
```

A connector that fails to load is reported and skipped; it does not stop the
platform. Its host services are installed with the platform's, from the active
release: `python -m sbx_control.host list|install|apply KEY` (`sbx-apply KEY`
for a settings change's root-side effect).

## Catalog packages

A package is a folder with a `package.yaml` and scripts, of one of two kinds:

| Kind | What it is | Example |
| --- | --- | --- |
| `connection` | A server or account the installation saves once under a key, with inputs and a check. | `postgres-server`, `neon-project`, `aws-role` |
| `provision` | A per-sandbox resource made on a connection, with `create`, `delete` and optional `list` scripts. | `postgres-clone`, `neon-branch`, `aws-profile` |

Packages come from `catalog/` (shipped), `/etc/sbx/catalog/` (the
installation's: `sbx-admin catalog install DIR` on the host, or
`sbx catalog new|push|test` from a workstation), or a repository's own
`.sbx/services/<name>/`. Shipped providers share `catalog/_lib/sbx_provider.py`,
which each package carries as a symlink.

The package contract (every `package.yaml` key, what a script receives, the
naming and retry rules, and the push-and-test loop) is written once, in the
brief the server gives package-writing agents:
[package-brief.md](../control/sbx_control/template_files/package-brief.md)
(`sbx catalog brief`). This page covers only what a package author on the
installation side needs beyond it.

### Runtime and actions

A package's Python code (`runtime`, `actions`, `.py` checks) runs inside the
control service, so only installation packages may have it, and only for
trusted repositories.

- `runtime(context)` receives `sandbox`, `repo`, `env` (the sandbox's env so
  far), `folder` (its directory under `/run/sbx/ext/`), `resources` (one per
  service of the package: `key`, `outputs`, `connections`), `base_url`, `files` (the
  package's files) and `action_url(key, action)`. It returns `{"env": {...},
  "files": {path: text}}`.
- An action `name: file.py` calls `file.name(context)` with `sandbox`,
  `resource`, `token` and `body`, and returns a JSON object. The sandbox calls
  `POST /api/sandbox/actions/<sandbox>/<key>/<action>` with
  `Authorization: Bearer <token>`. The platform issues every resource of a
  package with `actions` its own token: its scripts get it as
  `SBX_ACTION_TOKEN`, and it is the resource's sealed `token` output. Calls
  are refused once the repository no longer attaches the service or its
  binding changed.

`aws-profile` uses all of these: `runtime` writes an AWS config whose
`credential_process` calls the `credentials` action, which assumes the
`aws-role` connection's role on the host. See
[AWS role access](../catalog/aws-role/README.md).

### Template layer

A provision package may add to the templates of the repositories that attach
it, so its tools are installed once instead of in every sandbox:

```yaml
template:
  apt: [postgresql-client]
  files:
    /etc/my-tool.conf: "setting = 1\n"     # or {content: ..., mode: 0644}
  root: ["curl -fsSL https://example.com/tool.sh | sh"]
  user: ["my-tool --version"]
```

It becomes the package's `Contribution` template layer for a trusted
repository's template, next to the platform's and the agent connectors'.
Attaching or detaching such a package, or changing its `template:`, changes the
template's fingerprint, so the template is rebuilt. Commands run without
credentials; per-sandbox values belong in `runtime`.

### Repository packages

A trusted repository can carry its own package in `.sbx/services/<name>/` and
use it as `uses: ./.sbx/services/<name>`. It runs in a throwaway sandbox from
the base template, sees only the repository's secrets, and cannot have a
`runtime`, `actions` or `requires`: it never sees the installation's connections.

## GitHub

GitHub is a platform connection built as an extension (`connections/github.py`,
routes in `github_api.py`). It contributes `gh` to the template layer, and to
trusted sandboxes either a deploy key or a GitHub App credential helper in the
runtime layer; `$HOME/.config/gh` and `$HOME/.git-credentials` are scrubbed from
reusable snapshots. Through `git_access` it is how template builds clone a
repository on the host. With the App installed, its webhook rebuilds templates
on pushes that change `.sbx/` and runs PR previews: a pull request labelled
`preview` gets a team-shared sandbox `pr-<number>` that follows the branch and
is deleted when the label goes or the PR closes. The App's id, key and secrets
are the installation's own secrets, not env variables.

## Runtime backends

A runtime backend runs sandboxes somewhere: Embed on this host, or a
provider's service. The built-in one is listed in `sbx_control.runtime`
(`{"embed": "sbx_control.runtime.embed:Embed"}`). Another is an installed
Python package that registers an entry point, named after the runtime's kind:

```toml
[project.entry-points."sbx.runtimes"]
my-provider = "my_package:MyProvider"
```

Unlike a connector, a runtime that `SBX_RUNTIMES` enables but no package
provides stops sbx at start, naming the kinds that exist. A backend:

- subclasses `runtime.base.Backend` and implements its compute part: its sandboxes
  (`create`, `connect`, `sandboxes`, `info`, `pause`, `kill`, ...), their
  templates, and what it can do (`capabilities`);
- inherits `Backend`'s host part, whose no-op defaults
  fit a backend with nothing on this host, and overrides only what it has;
- reads only its own keys from `/etc/sbx/runtime.env` (`credential_keys`),
  never another backend's;
- keeps no process-global configuration: no credentials in the process
  environment or in module state where another backend would read them;
- passes `sbx-admin runtime verify <kind>` on a real installation.

The [sandbox runtime](development.md#sandbox-runtime) section has the
interface in detail.
