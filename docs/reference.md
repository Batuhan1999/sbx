# Reference

The recipe format, environment and secrets, services, the sandbox lifecycle,
templates, previews and limits. For onboarding steps see the
[installation guide](agent-install.md); for the per-sandbox data rule and how to
verify it, the [acceptance checklist](agent-install.md#acceptance-checklist).

## Recipe

A repository's recipe is its `.sbx/` folder: `sbx.yaml` plus optional scripts.

| File | Runs | Purpose |
| --- | --- | --- |
| `sbx.yaml` | | How the platform runs the repository (below). |
| `setup` | before start, on create and prepare | Install dependencies, run migrations and development seeds. Must be executable and safe to repeat. |
| `start` | after `processes` | For apps that need a start script: start servers detached and return; may print the main PID. |
| `build` | in a template build | Prepare dependencies and caches for a prebuilt template. |
| `Dockerfile` | in a template build | Extra OS layers for a prebuilt template. |
| `snapshot` | before a reusable snapshot | Remove the app's secrets and writable data from a copy ([Amp](amp-orbs.md#project-snapshots)). |
| `services/<name>/` | per sandbox | A repository's own service package ([extensions](extensions.md#repository-packages)). |

Any other file is shipped with the recipe; the whole recipe is at `/run/sbx/app`
in the sandbox. A recipe holds at most 128 regular files and 1 MiB; symlinks and
submodules are refused.

### sbx.yaml

| Key | Meaning |
| --- | --- |
| `previews` | Ports that get links: `"": 3000` for the main one, `name: port` for more. A value may be `{port, access, path, link}`; `access` is `team` (anyone signed in, default) or `owner`. |
| `processes` | `name: command`, run under the sandbox supervisor from the workdir; output goes to `/tmp/sbx-<name>.log`. Bind web servers to `0.0.0.0`. |
| `health` | URLs that must answer before the app counts as ready. Without them a started app is `app_unverified`. |
| `health_timeout` | Seconds to wait for health (default 240). |
| `logs` | Extra log paths for `sbx logs`, besides `/tmp/sbx-setup.log` and the process logs. |
| `env` | Variables for the app (see [environment](#environment-and-secrets)). |
| `services` | Per-sandbox resources from catalog packages (see [services](#services)). |
| `secrets` | Names of values the app needs that only the owner can supply (API keys, account tokens); listed as missing until set. See [environment](#environment-and-secrets). |
| `resources` | The size of every sandbox of the repository: `cpu`, `memory_mb`, `disk_mb` (defaults 2, 4096, 8192), with or without `template:` (see [sandbox size](#sandbox-size)), and `swap_mb` (256–65536). |
| `build_resources` | Replaces `resources`' `cpu`, `memory_mb` and `disk_mb` when present; it is the same size, not a separate build budget. |
| `lifecycle` | `{resume: preserve}` (default) keeps processes across pause; `{resume: restart}` restarts the app after resume, for apps whose connections go stale. |
| `template` | Present and non-empty: build a prebuilt template for this repository (see [templates](#templates)). Absent: start from the default template and clone. |
| `workdir` | Where the checkout is; default `/home/user/<repository-name>`. |

Read the [resource guide](../control/sbx_control/template_files/resource-guide.md)
before setting `resources` and `build_resources`; a requested size is not proof
that the runtime allocated it.

### Where the platform reads the recipe

The control service uses, in order, the operator's copy in
`/etc/sbx/apps/<owner>/<repo>/.sbx` (written by `sbx recipe apply DIR --repo OWNER/REPO`, admins only)
or a copy imported from the repository's default branch under `/var/lib/sbx/apps`
(refreshed by repository setup and template builds). An operator copy supplies
both manifest and scripts, so a checkout cannot mix in an older setup script.
Without one, the checkout's `.sbx` scripts take precedence over the imported
copy. Applying a recipe is a way to try it before committing:

```sh
sbx recipe apply /path/to/.sbx --repo owner/repo
sbx up first-check --repo owner/repo --branch COMMIT --json
sbx sandbox resources first-check
```

`sbx recipe rm --repo owner/repo` (`DELETE /api/repos/OWNER/NAME/recipe`, or
`sbx-admin recipe rm OWNER/REPO` on the host) removes the operator's copy; it
is refused while the repository has sandboxes. The repository stays served
and reads its default branch's `.sbx/` again, or needs a recipe; a template
it no longer has a use for is deleted with it.

`sbx recipe pr` publishes reviewed `.sbx` files as a draft pull request through
the GitHub App.

## Environment and secrets

Precedence, lowest to highest: service package defaults, recipe `env`,
installation values, repository values, personal values. Resolved per-sandbox
values (service outputs, generated secrets, preview URLs and values built from
them) always win over shared or personal defaults, so a shared database URL
cannot replace a sandbox's own.

A value may reference:

| Reference | Value |
| --- | --- |
| `${secret:NAME}` | A value stored for the repository (`sbx env set`). A launch stops while it is missing. |
| `${services.KEY.OUTPUT}` | An output of the sandbox's service `KEY`. |
| `${SBX_NAME}` and other sandbox metadata | The sandbox's own facts. |
| `${SBX_URL}`, `${SBX_URL_<PREVIEW>}` | The preview origin: `https://<sandbox>.<domain>`, or `https://<ip>:<port>` on an IP-only server. Use these for origins, callback URLs and CORS. |
| `${random:32}`, `${random:hex64}`, `${random:base64:32}` | A generated value, stable for the sandbox. |

Declare every value sbx cannot generate or provision, either by name under
`secrets:` (the app gets it under that name) or as `${secret:NAME}` inside
another value. The declarations are how sbx knows what is missing:
`sbx repo status` (`secrets_missing`) and the repository's **Environment** page
list declared names that have no value. Set values with `sbx env set` /
`sbx env import` or on that page. New values are secret by default;
choose plain visibility explicitly. A value's audience (`all`, `previews`,
`sessions`) and whether agent shells see it are separate settings; the app gets
its values before setup and start, interactive shells only those allowed for
agents.

Installation values are the operator's: the `variables` settings section
(`sbx-admin config set variables JSON`) maps repository patterns to variables,
and their `${secret:NAME}` resolves from the repository's values, then the
installation's own secrets. An admin sets those with
`sbx env set NAME --installation` (`ls` and `rm` take `--installation` too); a
change reaches the sandboxes of every trusted repository.

### Secret storage and live updates

The installation's secret store holds repository and personal values,
installation values, connection credentials, platform credentials and generated
per-sandbox secrets; SQLite keeps only metadata, access policy, resource
ownership and references. Which store is used is an installation setting,
`store.mode`, not something users choose in the web app:

| Mode | Store |
| --- | --- |
| `infisical` | A private Infisical on the host (loopback port 8190), set up by the installer. |
| `local` | Values sealed in `sbx.db` with the recovery key. `sbx-admin store enable` moves them into Infisical. |

`sbx-admin store status` reports the store and pending deliveries without
values. Adding a backend is described in
[development](development.md#secret-store-backends).

A change made in sbx is delivered to running sandboxes immediately; a direct
Infisical edit arrives through a signed webhook, or a 60-second poll. sbx's
own writes to the store (values it sets, payloads it seals for services and
connections) count as seen, so the poll does not bring them back as edits. sbx
rewrites the sandbox's private env files and, by default, restarts the app; it
does not rerun setup or recreate resources. Failed deliveries are retried;
paused sandboxes receive changes on resume. A value set while the sandbox
runtime does not answer is stored, and `sbx env set` says its delivery is
pending; the keeper delivers it once the runtime answers. `sbx env set --no-restart` updates
the files only until the next start.

Apps with their own secret loader keep using it through their own identity; sbx
does not copy that source into its store. For AWS, see
[AWS role access](../catalog/aws-role/README.md).

## Services

A service is a per-sandbox resource made by a catalog package, usually on a
connection (a saved server or account) set up once. Choose packages with
[service selection](service-selection.md); write new ones with
[extensions](extensions.md#catalog-packages).

```yaml
services:
  db:
    uses: postgres-clone          # the package
    connections: {server: dev-pg} # its required connection, when there is more than one
    with: {template: template0}   # its inputs
env:
  DATABASE_URL: ${services.db.url}
```

A service takes only `uses`, `connections` and `with`; a recipe with any other
key is refused when it is installed.

`sbx attach PACKAGE` adds an attachment from the CLI; `sbx detach KEY` removes
it. Resources are created before `.sbx/setup` runs. Each attachment gets a
stable resource ID recorded before creation, so retries keep the same resource.
Deleting a sandbox deletes the resources it owns and keeps the shared servers.
Replacing a service's package or connection requires deleting its resource first.

### Managed shared servers

The host can run shared Postgres and ClickHouse servers as Docker services on a
private network outside the sandbox VMs:

```sh
sbx-admin server up dev-pg --kind postgres
sbx-admin server up dev-ch --kind clickhouse
sbx-admin network apply      # before creating sandboxes that use the network
sbx-admin server ls
```

Sandboxes of trusted repos reach two kinds of private range: the operator's
`network.internal_cidrs` (`install.sh --internal`, `sbx-admin config set
network`) and, while a managed server exists, the managed servers' network
(`172.29.50.0/24`). sbx computes the second where it renders the allowlist
(Embed's slot firewall and the host's forwarding rules), so `server up` never
writes the operator's setting and changing that setting never drops a managed
server's network. The first `server up` asks for the host's routes and leaves
`sbx-admin network apply` pending for Embed. Managed servers and
`access.public_repos` exclude each other, as `internal_cidrs` and
`public_repos` do: the runtime opens private ranges to every sandbox alike.
A runtime whose sandboxes can't reach this host's private ranges refuses
`internal_cidrs` and managed servers; one that can't apply per-sandbox egress
rules refuses `public_repos`.

Managed Postgres is PostgreSQL 18 on an owned, reflink-enabled XFS image
(default 32 GiB, `--storage-gb N`, or `--data-root DIR` on an existing XFS
filesystem), so `postgres-clone` copies a populated template database with
copy-on-write. Before registering the connection sbx checks that a copy shares
physical extents and that migrating the copy leaves the source unchanged;
`sbx-admin server check NAME` repeats that check. The image is preallocated and
not resized; budget it and the server's memory separately from sandbox
resources. The server speaks plain TCP on that private network, so its
connection URL, and each `postgres-clone` URL made from it, carries
`sslmode=disable`; clients that default to TLS (Go's `lib/pq`) need nothing
else. A PostgreSQL 17 data volume is never reused: create a new server and
migrate with `pg_dump`/`pg_restore`.

`clickhouse-database` creates empty schemas by default; with `mode: clone` and a
`template`, it clones each supported local MergeTree table, sharing immutable
data parts. Views and replicated, shared or external engines are refused.

`sbx-admin server down NAME` stops a server and keeps its data;
`--delete-data` removes its storage. Both refuse while recipes or sandboxes use
the server.

### Development baselines

To start sandboxes from preloaded development data instead of an empty
database, prepare a baseline database on the shared server and name it as the
package's `template`. `sbx-admin server exec` runs SQL with the server's stored
admin access, so no admin password is copied:

```sh
sbx-admin server exec dev-pg <<'SQL'
CREATE ROLE development_owner NOLOGIN;
CREATE DATABASE development_seed OWNER development_owner TEMPLATE template0;
SQL
sbx-admin server exec dev-pg --database development_seed < development-seed.sql
```

Start the import with `SET ROLE development_owner;` and use dumps without owners
or grants (`pg_dump --no-owner --no-acl`), so sandbox logins can inherit
ownership through `owner_role`:

```yaml
services:
  db:
    uses: postgres-clone
    with: {template: development_seed, owner_role: development_owner}
```

For ClickHouse, import into a development database and use `template:` with
`mode: clone`. For Neon and other hosted providers, use the provider's own
import and select the development parent branch. Keep the baseline quiet while
clones are made; changing it affects future sandboxes only. Run ordinary
migrations and small fixtures in `.sbx/setup`, never destructive baseline
preparation.

## Lifecycle

| Operation | API | Behavior |
| --- | --- | --- |
| create | `POST /api/sandboxes` (`sbx up`) | Allocate the VM, provision services, prepare the checkout; returns when SSH works. Setup and start continue in the background unless `start_app=false`. |
| prepare | `POST /api/sandboxes/NAME/prepare` | Refresh the recipe and env, rerun setup and start; source edits are kept. |
| restart | `POST /api/sandboxes/NAME/restart` (`sbx sandbox restart`) | Restart the app; files and service state are kept. |
| pause / resume | `POST /api/sandboxes/NAME/pause`, `/resume` | Keep VM state and resources; see `lifecycle.resume`. A pause the runtime refuses (`node is busy`: it is still saving the sandbox's parent, or its disk cannot hold the sandbox's whole memory plus 1 GB) is tried again for 20 seconds; then the sandbox stays running at its checkpoint and the request answers 503 with the likely cause. |
| delete | `DELETE /api/sandboxes/NAME` (`sbx down`) | Remove the VM and its owned resources. |
| env_update | (on a variable change) | Rewrite the env files; restart the app when a value changed, unless the change asked for no restart. |

Each sandbox has one lifecycle record (`GET /api/sandboxes/NAME/lifecycle`, also
in list and detail responses). It is the only source of a sandbox's status. An
operation moves through these phases; a transition the table does not allow is
refused:

| From | To |
| --- | --- |
| `queued` | the operation's first phase: create `allocating` or `provisioning_services`; prepare `preparing_workspace`; restart `preparing_workspace` or `starting_app`; resume `restoring_runtime`; pause `pausing`; delete `deleting`; env_update `preparing_workspace` |
| `provisioning_services` | `allocating`, `services_ready` |
| `allocating` | `preparing_workspace`, `workspace_ready` |
| `preparing_workspace` | `workspace_ready` |
| `workspace_ready` | `provisioning_services`, `services_ready`, `setting_up`, `starting_app` |
| `services_ready` | `allocating`, `preparing_workspace`, `setting_up`, `starting_app`, `app_ready`, `app_unverified` |
| `setting_up` | `starting_app` |
| `starting_app` | `app_ready`, `app_unverified` |
| `restoring_runtime` | any preparation phase or checkpoint |
| `pausing` | any checkpoint except `deleted` |
| `deleting` | `deleted` |

An operation finishes at a checkpoint (`workspace_ready`, `services_ready`,
`app_ready`, `app_unverified`) or at `failed`, `interrupted` or `deleted`. App
work (`setting_up`, `starting_app`) requires both the workspace and services
checkpoints. Only one operation runs per sandbox: a second one gets HTTP 409.
Operation IDs fence late completions, also when a name is reused.

Derived from the record:

| Field | Values |
| --- | --- |
| `status` | `creating`, `starting`, `paused` (no operation runs and the VM is paused), `up`, `workspace_ready`, `failed`, `deleted`, `unknown` |
| `readiness` | `not_ready`, `workspace_ready`, `services_ready`, `app_ready` (on the bound, running VM) |

Readiness is a checkpoint: the health URLs are checked when an operation
starts the app, not after. What the keeper checks every minute is that a ready
sandbox's `processes` still run under sbx's supervisor; when they do not, the
sandbox's status becomes `failed` with the reason (for example its `/run`, a
small tmpfs that holds sbx's env and the supervisor's state, is full, and what
fills it), and `restart` starts the app again. An operation that fails on a
full `/run` names it the same way. When the control service restarts, unfinished operations become `interrupted`; their checkpoints and
owned resources are kept, and nothing is replayed. Inspect the record and retry
`prepare` or `restart`; setup holds a lock in the VM, so a retry cannot overlap a
command that survived the crash.

## Templates

A template is composed from three layers:

1. the **platform layer**: the OS, SSH and its WebSocket bridge, the desktop,
   Node, Chromium and the start contract (the `sbx-base` template);
2. **contributions** of the extensions: `gh` when GitHub is connected, each
   agent connector's CLI (Claude Code, Codex for `sbx open codex`), and the
   template layers of catalog packages a trusted repository attaches
   ([extensions](extensions.md#contributions));
3. the **recipe**: `.sbx/Dockerfile`, the checkout and `.sbx/build` when
   `sbx.yaml` sets `template:`; otherwise common toolchains (the `default`
   template).

The fingerprint covers all three layers; of the recipe, what the build
reads: `.sbx/Dockerfile`, `.sbx/build`, `workdir` and the size. Editing
`setup`, `start`, helper scripts, configs or checks rebuilds nothing; they run
in sandboxes. What a build script runs from the checkout is the repository's
content, which the nightly rebuild follows. The platform names a template
`<repo-slug>-<fingerprint[:8]>`, so a changed input produces a new template and
never overwrites one that sandboxes use. A template is stale when the plan it
would build now has a different fingerprint from the last build; the scheduler
checks every five minutes and builds stale templates one at a time, and at 02:00
host time rebuilds repositories whose default branch moved. Sandboxes start from
the template their repository built last when it has one of its own, else the
default one of their size. A create before that template exists starts its
build and is refused with 503 ("the ... template is building", `Retry-After`);
the repository's readiness shows the template as not built meanwhile. After a
failed build the 503 names the failure and nothing restarts it on its own.

A build starts only when the host has room for it, and is refused before
anything starts otherwise, saying what is missing (`POST
/api/repos/OWNER/NAME/template` and a create answer 503; the scheduler tries
again on its next round):

- free sandbox memory (hugepages) for twice the template's memory: each build
  step starts from the previous step's snapshot while that one still holds
  its memory;
- free disk where Embed keeps templates (`/var/lib/e2b`) for two of its
  largest steps, 2 × (memory + 4 GiB). Embed stores every step twice, in
  `/var/lib/e2b/storage/templates` and in its orchestrator's
  `/orchestrator/build`, so a large app's build needs far more while it runs:
  n8n's (15 steps at 8 GiB) used over 60 GB.

Embed cannot stop a build once it runs (deleting its template lets it run to
the end), and a disk that fills during one crashes Embed's orchestrator and
the sandboxes on it. A build that fails while the disk or the memory runs low
says so in its error instead of Embed's "internal error" or a refused
connection.

Templates nothing starts from are deleted from Embed: an earlier build of a
template (its replacement has another name), the default template at a size
no served repository asks for any more, and the template of a repository
that has none any more; never one a running or paused sandbox started from.
The scheduler does this every round and after each build; `sbx-admin template
prune` does it now. Their layers go with the storage cleanup
(`e2b-storage-gc`), which runs hourly and after every build: layers nothing
reads go at once while Embed writes no build or snapshot, else once they are
two hours old. `sbx template rm` (`DELETE /api/repos/OWNER/NAME/template`, or
`sbx-admin template rm KEY` for any key: `OWNER/REPO`, `default`,
`default:<size>`, `sbx-base`) deletes a template's builds and its build
record, a failed one's included; a repository whose recipe still asks for a
template builds it again.

### Sandbox size

The runtime sizes a VM by the template it starts from; creating a sandbox
cannot change it. So the platform builds every template at the size of the
sandboxes that start from it: a repository's own template at its recipe's
size, and the default template once per size a served repository without
a template of its own asks for
(`default` at 2 vCPU, 4 GiB RAM and 8 GiB free disk; another size as
`default:<cpu>cpu-<memory>mb-<disk>mb`). A recipe's `resources` therefore apply
whether or not it sets `template:`. A changed size is a new template; the
scheduler builds it within minutes, or `sbx template build` builds it now.
Until it is built, a repository's own template built earlier serves at its
old size; with none, new sandboxes are refused rather than started at another
size.

Host capacity counts the memory each sandbox actually gets: the size its
template was built with. A sandbox started before a size change keeps its old
size; its resources snapshot reports `allocation_below_recipe`.

Build one now with `sbx template build` in a checkout (`--repo owner/repo`
elsewhere, `--wait` to follow it) or `POST /api/repos/OWNER/NAME/template`;
`sbx template status` and `GET` on the same route report `building`, `built` or
`failed` and whether it is stale. `sbx repo connect --prebuild` scaffolds a
starting point.

A template never contains credentials or a sandbox's mutable data. The checkout
reaches its build as a git bundle served once under a random token. On an
installation where builds cannot reach the normal URL, set
`SBX_TEMPLATE_BUNDLE_ORIGIN` to a build-reachable HTTP(S) origin of the same
service (origin only: no credentials, path, query or fragment). It changes no
listener or firewall rule.

## Runtimes

A runtime is where sandboxes run: E2B Embed on this server, or a provider's
service. An installation runs the runtimes it enables, and each sandbox runs
on exactly one of them, chosen when it is created and kept until it is
deleted. Two are built in, `embed` and `e2b-cloud` (below); other runtimes
are installed as packages ([runtime backends](extensions.md#runtime-backends)).

A fresh install enables Embed where the server can run it (`/dev/kvm`,
16 GiB of memory, 24 for the default budget, and 60 GiB of free disk), and
E2B Cloud when `E2B_CLOUD_API_KEY` is in `install.sh`'s environment. A
server that can run neither stops the installer with what to set. An
upgrade keeps the runtimes the installation enables.

### E2B Embed (`embed`)

E2B's self-hosted stack on this server: Firecracker VMs in hugepage memory,
templates and snapshots under `/var/lib/e2b`. `install.sh` sets it up and
writes its keys (`E2B_API_KEY`, `E2B_API_URL`, `E2B_SANDBOX_URL`; optional
`E2B_HOST`, `E2B_DOMAIN`). Its sandboxes reach the ranges this host opens to
them (`private_networks`: managed servers, `network.internal_cidrs`), it
applies each sandbox's `deny_out` and takes filesystem snapshots. Its limits
are the host's: its cores and the hugepages per sandbox; how many run at once
is the room left.

### E2B Cloud (`e2b-cloud`)

E2B's hosted sandboxes, through the same SDK as Embed. Nothing of them runs
here: the server needs no KVM, about 2 GiB of memory and 10 GiB of disk for
sbx-control, nginx and the secret store.

| Key in `runtime.env` | Meaning |
| --- | --- |
| `E2B_CLOUD_API_KEY` | The API key from E2B's dashboard (a secret; sealed into the store once that is Infisical). Required. |
| `E2B_CLOUD_DOMAIN` | E2B's domain; default `e2b.app`. |
| `E2B_CLOUD_MAX_TIMEOUT` | Seconds a sandbox runs before its timeout pauses it; default 3600 (the Hobby plan's most). The keeper renews it while the sandbox is used. |
| `E2B_CLOUD_MAX_CPU`, `E2B_CLOUD_MAX_MEMORY_MB`, `E2B_CLOUD_MAX_DISK_MB` | Per sandbox; default 8 vCPUs, 8192 MiB, 10240 MiB (Hobby). A recipe beyond them is refused, naming the key. |
| `E2B_CLOUD_MAX_SANDBOXES` | Running at once; default 20. |

Raise the limits to what your E2B plan allows, then `sbx-admin runtime
apply`.

What it needs:

- **A domain reachable from the internet.** Its sandboxes call this
  installation back at `login.<domain>` on 443: the template builds' repo
  bundles, git credentials, catalog actions. The installer refuses E2B Cloud
  without a domain; the setup's reachability check covers the rest. With
  `--dns none` the certificate is self-signed: sbx's own calls from a
  sandbox (bundles, git credentials) pin its key, anything else in a sandbox
  that calls the server refuses it.
- Previews go to `https://<port>-<sandbox id>.<E2B domain>` with that
  sandbox's traffic token, which E2B returns only when a sandbox is created
  or connected: sbx keeps it, and after sbx-control restarts fetches it once
  by connecting (which resumes a paused sandbox, as a preview request would).
  nginx adds it for E2B Cloud's sandboxes only; it never reaches the browser
  or another runtime.

What it does not support: managed servers and `network.internal_cidrs`.
Its sandboxes do not reach this host's private network
(`private_networks` is false), so a repository with managed servers
attached is refused on it, naming the servers. It applies `deny_out`
(`egress_rules`), so untrusted repos can run there, and takes filesystem
snapshots.

### Enabling runtimes

The runtimes are set in `/etc/sbx/runtime.env` (root only, not in backups):

| Key | Meaning |
| --- | --- |
| `SBX_RUNTIMES` | The enabled runtimes, by kind, in order: `SBX_RUNTIMES=embed`. Missing or empty means `embed`. |
| `SBX_RUNTIME_DEFAULT` | The runtime of new sandboxes when nothing else chooses one. Missing means the first in `SBX_RUNTIMES`; it must be one of them. |

Each runtime reads only its own keys from the same file; two runtimes never
share a key name. A kind that no installed package provides, or a default
that is not enabled, stops sbx at start, naming the kinds that exist.

Nothing watches the file. sbx-control configures each runtime when it starts,
and nginx has one upstream per enabled runtime, written by the TLS step. After
editing `runtime.env`, run `sbx-admin runtime apply`: it checks the file (the
same checks as at start), then regenerates nginx and restarts sbx-control. An
upgrade (`install.sh`) does both as well. Or let `sbx-admin` edit it:

- `sbx-admin runtime add KIND` enables a provider's runtime with its API key,
  read from stdin, never the command line (`sbx-admin runtime add e2b-cloud <
  key-file`, or type it and press Ctrl-D). It checks the key with the
  provider first and changes nothing when the provider refuses it; given
  again, it replaces the key. Embed is the installer's: it needs the host set
  up.
- `sbx-admin runtime remove KIND` disables one, refused while it has
  sandboxes (running or paused) or when it is the only one. A provider's
  keys leave the file.
- `sbx-admin runtime default KIND` sets `SBX_RUNTIME_DEFAULT`.

Each then does what `runtime apply` does.

A new sandbox's runtime is, in order: the one the request names
(`sbx up NAME --runtime KIND`, or `runtime` in `POST /api/sandboxes`), the
repository's setting (admins: `PUT /api/repos/OWNER/NAME/runtime`, or the
Recipe & template tab of the repository's page), then the installation's
default. sbx then checks the choice and refuses with the reason, naming the
runtime, instead of picking another:

- the runtime is enabled (a repository setting that names a runtime no
  longer enabled is refused too, naming the setting to change, rather than
  moved to the default);
- managed servers attached to the repository need a runtime whose sandboxes
  reach this host's private ranges (`capabilities.private_networks`);
- the recipe's `resources` fit the runtime's limits.

Templates are built per runtime. The scheduler builds the base and default
templates on every enabled runtime, and a repository's own template on the
runtimes its sandboxes use: its setting, the default, and any runtime one of
its sandboxes runs on.

A sandbox cannot move to another runtime. To switch, delete it and create it
again on the other one.

- `sbx runtimes` and `GET /api/runtimes` list the enabled runtimes: each one's
  `kind`, `title`, whether it is the `default`, whether it runs on this server
  (`host`), its `capabilities`, its `limits` (`{}` when it states none) and its
  `status` (`state` and `summary`). The settings page shows the same.
- Sandbox payloads (`GET /api/sandboxes`, `GET /api/sandboxes/NAME`) carry
  `runtime`, the kind the sandbox runs on. The web app's sandbox list
  (`GET /api/sandbox-list`) also has `capacity`, the default runtime's room
  with its `runtime`, and with several runtimes `capacities`, each one's by
  kind.
- `GET /api/repos/OWNER/NAME/template` describes the template on the
  repository's runtime (`runtime`); with several runtimes, `runtimes` lists
  the same for each other one. `POST` on it builds on the repository's
  runtime.
- `GET /api/repos/OWNER/NAME` carries `runtime`, the repository's setting, or
  `null` for the installation's default.
- `GET /api/setup/checks`, the first-run wizard's, names the runtime each
  check is about (`runtime`, `null` for the server's own). The wizard and
  `GET /api/runtimes` take the setup link's token.
- On the host, `sbx-admin runtime list` (`--json` as `GET /api/runtimes`
  answers) shows the enabled runtimes, and `sbx-admin runtime verify [KIND]`
  (the default when no kind is given) checks one end to end: it creates a
  sandbox from the base template, runs a command, writes and reads a file,
  pauses and resumes it, reaches a preview port through the gate and deletes
  it, reporting each step.

## Previews

With a domain, the main preview is `https://<sandbox>.<domain>` and the others
`https://<sandbox>-<name>.<domain>`.

Without a domain, sbx assigns each sandbox and preview port a stable
`https://<ip>:<port>` from TCP 21000–21255 (`network.ip_preview_first_port` and
`network.ip_preview_last_port`, changed with `sbx-admin config set network JSON`). The
same renewable IP certificate covers the control plane and the preview ports.
Keep the range clear of other listeners; shrinking it cannot drop assigned URLs.

In both modes the gateway checks the sign-in and the preview's `access`, keeps
HTTP streaming and WebSockets, and strips sbx's login cookie and API token
before proxying. A host that names no sandbox, a deleted one or a port it does
not expose answers 404; an owner-only preview answers 403 to anyone else. It does not rewrite the app: use `${SBX_URL_*}` for origins and
callbacks. Browser cookies are per host, not per port, so on an IP-only server
give the app a cookie prefix with `${SBX_NAME}` to keep simultaneous sandboxes'
logins apart; `sbx_session` is reserved.

Declared preview URLs survive restart and pause and are released on delete.
Ports the platform detects without a declaration are reclaimed after five
minutes of successful scans without them.

PR previews: with the GitHub App connected, a pull request labelled `preview`
gets a sandbox running its branch and a comment with its links. New commits
update it; removing the label or closing the pull request deletes it. Verify
creation, updates, working links and cleanup per repository before relying on
it.

## HTTP API

The desktop CLI and the web app use the control service's HTTP API at the
installation's address (its base URL); any HTTP client can. Requests carry
`Authorization: Bearer <token>`. A token acts as the email it was made for:
admin routes need one of the installation's admins (`access.admins`).

- `sbx login` (the desktop CLI, macOS) keeps a token in the Keychain.
- On the host, for Linux, CI or an agent without the desktop CLI:

  ```sh
  sbx-admin token create you@example.com --label ci   # prints the token once
  sbx-admin token ls                                  # id, email, label, last use; never values
  sbx-admin token revoke ID
  ```

  Keep the token in a secret store, never in a repository. A revoked token
  stops working for the API at once and for previews within a minute.

```sh
curl -fsS -H "Authorization: Bearer $SBX_TOKEN" https://sbx.example.com/api/me
```

Errors are JSON `{"detail": "..."}`: 400 a bad request, 401 no valid token, 403
not allowed, 404 none such, 409 a conflicting operation (another one runs,
the template is building), 503 not now (the runtime is starting, a template is
building or has no room), with `Retry-After` when waiting helps.

| Route | Who | What |
| --- | --- | --- |
| `GET /api/me` | anyone | The token's email, whether it is an admin, the installation's repositories. |
| `GET /api/repos`, `GET /api/repos/OWNER/NAME/readiness` | anyone | The repositories and one's setup checklist: what is missing and who acts. |
| `GET /api/repos/OWNER/NAME` | anyone | What the installation knows about a repository, with `runtime`: its setting, or `null` for the default. |
| `PUT /api/repos/OWNER/NAME/runtime` | admin | Body `{"runtime": "KIND"}` sets the runtime of the repository's new sandboxes; `{"runtime": null}` goes back to the installation's default. 400 for a runtime that is not enabled; answers the repository as `GET` does. |
| `GET /api/runtimes` | anyone, and the first-run setup link | `sbx runtimes`: the enabled runtimes and the default ([runtimes](#runtimes)). |
| `GET /api/repos/OWNER/NAME/setup-brief` | anyone | The setup brief an agent follows (text). |
| `PUT /api/repos/OWNER/NAME/recipe` | admin | `sbx recipe apply`: serve and trust the repository with this recipe, replacing the operator's copy. Body `{"files": {".sbx/sbx.yaml": "...", ".sbx/setup": "..."}, "executable": [".sbx/setup"]}`: every file of the recipe by its path in the repository, and which are executable. |
| `DELETE /api/repos/OWNER/NAME/recipe` | admin | `sbx recipe rm`; 409 while the repository has sandboxes. |
| `GET /api/repos/OWNER/NAME/files` | anyone | The recipe the platform reads, and where from. |
| `POST /api/repos/OWNER/NAME/template` | admin | `sbx template build` on the repository's runtime, in the background; 409 while it builds, 503 without room. |
| `GET /api/repos/OWNER/NAME/template` | anyone | Its status (`building`, `built`, `failed`), `stale`, the error, and for admins the build's last log lines; `runtime` names the runtime, and with several `runtimes` lists the other runtimes' states. |
| `DELETE /api/repos/OWNER/NAME/template` | admin | `sbx template rm`. |
| `GET /api/repos/OWNER/NAME/env`, `PUT /api/repos/OWNER/NAME/env/VAR` | admin (anyone with `"mine": true`) | `sbx env ls`, `sbx env set`; body `{"value": "...", "secret": true}`. |
| `POST /api/sandboxes` | anyone | `sbx up`: body `{"name": "...", "repo": "OWNER/NAME", "branch": "...", "public_keys": ...}`; `branch` defaults to the default branch, `public_keys` is `authorized_keys` text or a list of keys (optional), `runtime` a kind (optional, see [runtimes](#runtimes)); a runtime that cannot take the sandbox is a 400 naming it and why. Returns when SSH works; `?start_app=false` stops before setup. |
| `GET /api/sandboxes`, `GET /api/sandboxes/NAME?wait=SECONDS` | owner | Your sandboxes; one, with its lifecycle record and preview URLs. `wait` (at most 60) returns once its operation moves on. |
| `POST /api/sandboxes/NAME/exec` | owner | Run a script as the sandbox's user: body `{"script": "...", "timeout": 120}`, answers `exit_code`, `stdout`, `stderr`. |
| `GET /api/sandboxes/NAME/inspect?lines=N` | owner | The checkout's commit and the tails of the setup and process logs. |
| `POST /api/sandboxes/NAME/restart`, `/prepare`, `/pause`, `/resume`; `DELETE /api/sandboxes/NAME` | owner | The [lifecycle](#lifecycle) operations. |
| `GET /api/updates`, `POST /api/updates`, `POST /api/updates/check` | admin | The installed release, the newer ones with their notes, and the last upgrade with its steps and progress; `POST` with `{"tag": "v0.3.0"}`, the newest release, [installs it](#operating-the-host): 400 for any other tag, 409 while an upgrade runs. `check` asks GitHub now. |

## Operating the host

- `sbx-admin backup` writes a bundle with SQLite, the local Infisical database,
  sealed bootstrap files and the operator's recipes and packages
  (`/etc/sbx/apps`, `/etc/sbx/catalog`); keep its recovery key separately.
  `sbx-admin restore` puts a bundle back on a fresh host. `sbx-backup.timer`
  writes one on a schedule, whatever the secret store: the `backup` settings
  section (`every_hours`, default 24, 0 for never; `to`, default
  `/var/lib/sbx/backups`; `keep`, default 14) sets how often, where, and how
  many scheduled bundles are kept; bundles made by hand stay. VMs, templates,
  shared-server data and hosted databases need their own backups.
- sbx runs no operator hooks. Catalog
  packages provision per-sandbox resources, the secret store holds values, and
  backups cover the installation's state.
- `sbx-admin network apply` applies private service networks; it refuses to
  recreate the runtime while sandboxes exist. A sandbox created while the
  runtime is still starting gets `503` with `Retry-After`, and nothing of it is
  kept: send the same request again.
- An installation runs the release it was installed from: a checkout of a
  release tag (`v0.2.0`), the release's archive, or `main` for the newest,
  unreleased changes. `/opt/sbx-control/current/VERSION` and the web app name
  it. To upgrade to, or pin, another release, read its
  notes on the [releases page](https://github.com/Batuhan1999/sbx/releases)
  (Breaking changes and upgrade steps come first), then install from its
  archive, which carries the web app built:

  ```sh
  v=v0.2.0 url=https://github.com/Batuhan1999/sbx/releases/download
  curl -fsSLO "$url/$v/sbx-$v.tar.gz" && curl -fsSL "$url/$v/sbx-$v.tar.gz.sha256" | sha256sum -c \
    && tar -xzf "sbx-$v.tar.gz" && sudo sh "sbx-$v/install.sh"
  ```

  or from a checkout of its tag, which builds the web app first and so needs
  Node 22 (`install.sh` refuses a checkout without it):

  ```sh
  cd sbx && git fetch --tags origin && git checkout v0.2.0 \
    && npm --prefix web ci && npm --prefix web run build && sudo sh install.sh
  ```

  Going back to an older release is the rollback below, not an install of it:
  a newer release may have migrated `sbx.db`.
- Admins see a newer release in the web app's sidebar (`v0.3.0 available`)
  and on **Settings → Updates**, which shows what changed in every release the
  upgrade passes (upgrade steps, breaking changes, the pull requests) and
  installs it with **Update**; breaking changes in any of them must be
  confirmed as read. While it runs, the page shows its progress: the
  download, each step `install.sh` announces, sbx-control's restart, the log's
  last lines. sbx-control asks GitHub again that the tag
  is the newest release, then hands it to the host: `sbx-apply-upgrade`, as
  root, downloads `sbx-<tag>.tar.gz`, checks it against its `.sha256`, unpacks
  it into `/var/lib/sbx/upgrades/sbx-<tag>/` and runs its `install.sh` (the
  upgrade below), logging to `/var/log/sbx-upgrade.log`. The page then shows
  the new release, or the failure with the log and, while the new release
  runs, its `rollback.sh`. **Check now** asks GitHub at once.
  `sbx-admin upgrade` lists the newer releases with their notes, and
  `sbx-admin upgrade TAG` installs the newest the same way and prints the log.
  One upgrade runs at a time and never to an older release; an installation
  that runs no release (a checkout before the first tag) is offered none.
  The `updates` settings section sets where releases come from: `check`
  (default on; off for an air-gapped or private installation, which then asks
  GitHub only for `sbx-admin upgrade`), `repository` (default
  `Batuhan1999/sbx`, or a public fork that publishes the same release
  archives) and `prereleases` (release candidates too, for a test
  installation), as in `sbx-admin config set updates '{"check": false}'`.
- Re-running `install.sh` upgrades the installation and keeps its settings:
  the settings options (`--domain`, `--repos`, `--internal`, ...) apply on a
  first install only and are ignored, with a note, on an upgrade, which also
  keeps Embed's hugepages unless `--hugepages` is given. Before it changes
  anything, an upgrade compares what it would lay into Embed's project (the
  runtime's image pins, sbx's compose override, the keys sbx owns in
  `/opt/e2b/.env`) with Embed's running containers, as `docker compose`
  does; when that recreates any of them while sandboxes exist, running or
  paused, it refuses and names them: sandboxes do not survive a recreated
  runtime, so delete them first.
  An upgrade stops `sbx-control` and the running connector services
  (`sbx-amp`, `claude-orchestrator`, ...) before it migrates `sbx.db`, after
  backing up the state as it was into `/var/lib/sbx/upgrade-backups/<time>/`:
  a copy of `sbx.db`, the unit files, the previous release's link and a backup
  bundle (`sbx-admin backup`, the secret store's data included). The installer
  prints `rollback: sh /var/lib/sbx/upgrade-backups/<time>/rollback.sh`, which
  switches back to that release, its units and `sbx.db`; it leaves Embed and
  sandboxes as they are and does not undo the secret store (restore the bundle
  for that). An upgrade that fails before the new release runs (Embed does not
  start, for instance below its 20 GiB of free disk) runs it itself, so
  sbx-control and the previews come back. The services start again with the new release; running sandboxes
  keep running. `sbx.db` and its write-ahead files are kept at mode `0600`.
- `sbx-admin config get [SECTION]` and `sbx-admin config set SECTION JSON` read and change installation settings.

## Limits

- The desktop CLI (`sbx`) runs on macOS only: it keeps its tokens in the
  Keychain and its installer refuses other systems. Elsewhere use the
  [HTTP API](#http-api) with a token from `sbx-admin token create`. The host must be Ubuntu
  24.04; with KVM to run sandboxes on it (Embed), without to run them at E2B
  Cloud.
- Recreating the runtime with existing sandboxes is not supported; pausing them
  first does not make it supported.
- Backups cover configuration and secrets, not VM snapshots, templates or hosted
  data.
- A deleted sandbox's pause snapshots stay on the host's disk until the next
  storage cleanup (`e2b-storage-gc`, hourly): Embed has no API that deletes
  them. While Embed writes a build or a snapshot, the cleanup keeps what is
  younger than two hours.
- Embed cannot stop a template build once it runs; sbx checks memory and disk
  before it starts one.
- The platform does not rewrite app code for previews, and it does not prove an
  app works beyond its configured health checks.
- Claude Code sessions need a Claude environment with self-hosted runners; other
  agents work over SSH and the CLI.
