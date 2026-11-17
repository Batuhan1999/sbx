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
A runtime whose sandboxes can't reach this host's private ranges (E2B
Cloud's) reaches managed servers and internal hosts only through
[endpoints](#endpoints); without a domain it refuses `internal_cidrs` and
managed servers. One that can't apply per-sandbox egress rules refuses
`public_repos`.

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

### Endpoints

Sandboxes on a runtime whose sandboxes don't reach this host's private
network (`capabilities.private_networks` false: E2B Cloud) reach a managed
server or an internal host through an endpoint: one TLS path that every
service and protocol shares, on the HTTPS port (443): there is no port to
open, in the host's firewall or a cloud provider's. Endpoints are off until
an admin adds one.

```sh
sbx endpoints                                   # each, its repos and state; the managed servers without one
sbx endpoints add --server dev-pg               # a managed server's, at dev-pg.<domain>
sbx endpoints add --name temporal --target 10.0.0.5:7233 --repo owner/app   # an internal host's
sbx endpoints grant temporal owner/other        # another repo may use it; revoke takes it back
sbx endpoints rm temporal                       # refused while sandboxes on a provider runtime use it
```

`add` and `rm` wait until the server has applied the change and exit
non-zero when it is refused or fails (`--no-wait` returns at once, `--json`
prints the API's answer). On the server, `sbx-admin endpoint
ls|add|rm|grant|revoke` does the same, and `sbx-admin doctor` lists them. An
endpoint needs a domain; its name is a host name label (a managed server's
name, with `_` as `-`, unless `--name` gives one), its target an IP address
and port (an internal host's within `network.internal_cidrs`).

**Who may use one** is per repository and explicit, whatever the
repository's trust: a managed server's endpoint is for the repositories
whose attachments bind to that server; an internal host's for the
repositories an admin granted it (`--repo`, `grant`, `revoke`). A public
repository (`access.public_repos`) may be granted an internal host's too.
The rule is kept with certificates, not addresses: each endpoint has a CA of
its own, nginx checks a client against that endpoint's CA only, and a
sandbox gets certificates only for the endpoints its repository may use.
A grant, a revoke, a new endpoint or a removed one reaches the
repository's running sandboxes within seconds, without restarting their
apps: the forwarder's configuration and certificates and
`SBX_ENDPOINT_<NAME>` in `/run/sbx/env` are written again (a paused
sandbox gets them when it resumes; `sbx sandbox restart` writes them too).
Processes already running keep the env they started with until they
restart; the forwarder follows its configuration by itself, and a revoked
endpoint's listener and certificates go.

How it works:

- **The host.** nginx's stream module owns port 443 and reads the TLS
  server name of each connection without decrypting it (`ssl_preread`): an
  endpoint's `<name>.<domain>` goes to a loopback listener (127.0.0.1:8441)
  and every other name to the HTTPS server, which listens on 127.0.0.1:8442
  and takes the client's address from the PROXY protocol (`real_ip_header
  proxy_protocol`), so the gate, logs and previews see it as before; with no
  endpoint, every name goes there. On the endpoints' listener, one server per
  endpoint, chosen by `server_name`, terminates TLS with the installation's
  wildcard certificate, requires a client certificate from that endpoint's
  CA (`ssl_client_certificate`, `ssl_verify_client on`) and proxies to the
  target; any other name's handshake is rejected. sbx-control records a
  change and touches `/run/sbx/apply/endpoints`; the root side (`sbx-apply
  endpoints`) writes `/opt/sbx-tls/nginx/endpoints.stream` and the CAs'
  certificates, tests and reloads nginx, keeps the configuration before when
  nginx refuses it, and records the outcome the API shows (`on`,
  `applying`, `failed`). nginx's main configuration
  (`/opt/sbx-tls/nginx.conf`, with a `stream` block) is mounted into its
  container; the upgrade that brings this layout makes the container anew
  once, and a rollback (`rollback.sh`) puts the release before's back. The
  sandbox's own egress rules (`deny_out`) deny only the cloud metadata
  service, so `<name>.<domain>:443` is reachable.
- **Certificates.** An endpoint's CA is made with it; its key is sealed in
  the secret store (sbx-control issues the sandboxes' certificates on
  demand), its certificate is public. A sandbox's certificate for an
  endpoint is valid 24 hours. It is issued whenever the sandbox's runtime is
  written (create, a cold boot, an env delivery; the keeper renews a running
  one with less than 12 hours left), pushed when sbx resumes the sandbox
  (the gate, the API, the keeper), and pulled by the forwarder itself before
  a new connection when less than an hour is left (`POST
  /api/sandbox/endpoint-certs` with the sandbox's own credential, the one its
  git credential helper uses), so a sandbox woken after a long pause by
  anyone renews on its first connection. Open connections keep the
  certificate they began with. Nothing is revoked: a certificate runs out,
  and a deleted sandbox or a revoked grant gets no new one. No key appears in
  a log, an event or an admin's API answer.
- **The sandbox.** The guest helper's forwarder (`sbx_guest.py forward`)
  listens on `127.0.0.1:<local port>` (17400 and up, one per endpoint) for
  each endpoint the sandbox may use and wraps each connection into TLS to
  `<name>.<domain>:443` with that endpoint's certificate, verifying the
  server as `<name>.<domain>` (against the public CAs, or the installation's
  own certificate with `--dns none`). Apps connect to 127.0.0.1 in plain
  text, so Postgres's `sslmode=disable` URLs keep working; the service's own
  login (`postgres-clone`'s per-sandbox role) stays on top.
- **The values.** On such a runtime, every `host:port` in a sandbox's env
  and its services' outputs that is the target of an endpoint it may use (a
  URL, an address, or `host` and `port` outputs) becomes the forwarder's
  `127.0.0.1:<local port>`: `postgres-clone`'s `DATABASE_URL`, ClickHouse's
  URL, a Temporal address. Each internal host's endpoint is also
  `SBX_ENDPOINT_<NAME>=127.0.0.1:<port>` (`temporal`: `SBX_ENDPOINT_TEMPORAL`).
  Values must name an IP address with its port. Embed's sandboxes keep the
  private addresses.
- **The gate.** A repo's sandboxes are refused on such a runtime while a
  private target they use is not theirs to reach: a managed server its
  attachments bind to without an endpoint, or an address within
  `network.internal_cidrs` in a bound connection's values or in `sbx.yaml`'s
  `env` without an endpoint or a grant. The refusal names the command
  (`sbx endpoints add --server dev-pg`, `sbx endpoints grant temporal
  owner/app`). Hosted packages (Neon, PlanetScale, Upstash, ...) need none.

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
deleted. Three are built in, `embed`, `e2b-cloud` and `boat` (below); other runtimes
are installed as packages ([runtime backends](extensions.md#runtime-backends)).

A fresh install enables Embed where the server can run it (`/dev/kvm`,
16 GiB of memory, 24 for the default budget, and 60 GiB of free disk), and
E2B Cloud when `E2B_CLOUD_API_KEY` is in `install.sh`'s environment. A
server that can run neither installs with no runtime enabled
(`SBX_RUNTIMES=''`): add one in the first-run wizard's "Sandbox runtime"
step, in Settings → Sandbox runtimes, or with `sbx-admin runtime add
e2b-cloud` ([no runtime yet](#no-runtime-yet)). An upgrade keeps the
runtimes the installation enables.

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
| `E2B_CLOUD_MAX_SANDBOXES` | Running at once; default 10 (Hobby). |

Raise the limits to what your E2B plan allows: in Settings → Sandbox
runtimes (Change settings, then a plan or Custom), or in the file and then
`sbx-admin runtime apply`. The web app offers E2B's plans as presets, from
[E2B's pricing](https://e2b.dev/pricing) as read on 2026-11-17: Hobby (1 h,
8 vCPUs, 8192 MiB, 10240 MiB of disk, 10 at once) and Pro (24 h, 8 vCPUs,
8192 MiB, 20480 MiB, 100 at once). The defaults above are Hobby's, so an
installation that never chose a plan is on Hobby. A save in the web app sends
only what was changed (or what a newly chosen plan sets): a limit raised by
hand in `runtime.env` beyond the form's range is left as it is.

When E2B refuses a timeout past the plan's ("Timeout cannot be greater than
1 hours"), sbx says so, naming the setting to change ("E2B refused a 2 h
timeout: your plan allows 1 h; change Longest run (seconds)
(E2B_CLOUD_MAX_TIMEOUT) in Settings → Sandbox runtimes → E2B Cloud ..."), and
shows it on the runtime's row (`limit_hit` in `GET /api/runtimes`) until its
settings change. Any other refusal (too many sandboxes at once, a sandbox
larger than the plan allows) shows E2B's own message. sbx never raises a
limit by itself.

The domain (`E2B_CLOUD_DOMAIN`) must be a host name, and changing it in the
web app needs the API key typed again: the key set is never sent to a new
domain.

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
  or another runtime, and the gate's answers that carry it are not cached on
  disk. sbx creates every E2B Cloud sandbox with private ports
  (`allow_public_traffic` off, whatever the E2B project's default) and
  deletes one that comes back without a token. A CORS preflight wakes
  nothing: without a token sbx already holds, it is refused, and the
  browser's real request resumes the sandbox.
- nginx resolves the ingress name (`sbx-ingress.<E2B domain>`) when it loads
  its configuration: a DNS failure then fails the reload (`nginx -t` says
  which name), and an address E2B changes later is picked up at the next
  reload (`sbx-admin runtime apply`, or any certificate renewal).

Several installations may share one E2B key (one E2B team). Each keeps to
its own: every installation has a random id, made once and kept in `sbx.db`
(so backups and upgrades keep it; it is not a secret), and every sandbox sbx
creates carries it in its metadata (`sbx_install`). On a runtime that does
not run on this server, sbx lists sandboxes by that id (the provider
filters), so it never sees, finds, counts, adopts or deletes another
installation's, even one of the same name; and it names its templates and
snapshots with the id's first 8 characters (`<id8>--sbx-base-<hash>`),
listing and pruning only those. Sandboxes made before the id was added are
this installation's when its records know them by id (taken once per
runtime, at the first listing after the upgrade); any other untagged
sandbox on the account is left alone. Templates built before are rebuilt
under the new name at the scheduler's next round (each once, the base
first: a template that starts from the base waits for it, and a create
meanwhile answers that its template is building); the old ones
are no longer pruned by sbx, so delete them in E2B's dashboard once
nothing uses them. A template that disappears from the provider (another
installation's prune, a manual delete) is rebuilt too: the scheduler
checks each round, and a create that finds it gone answers "the KEY
template is building ...; try again in a few minutes" (503) and starts the
build. The status line counts this installation's running sandboxes; the
plan's limit is the E2B team's, shared with the others. Embed runs one
installation per server: its sandbox listing and template names are as
before.

Its sandboxes do not reach this host's private network
(`private_networks` is false): they reach managed servers and
`network.internal_cidrs` hosts through [endpoints](#endpoints), and a
repository that uses one without an endpoint is refused on it, naming the
command that adds it. It applies `deny_out`
(`egress_rules`), so untrusted repos can run there, and takes filesystem
snapshots.

### Boat (`boat`)

[Boat](https://docs.boat.dev)'s hosted Ubuntu 24.04 sandboxes, through
Boat's Python SDK (`boat-sdk`). Like E2B Cloud, nothing of them runs here,
and they need a domain reachable from the internet.

| Key in `runtime.env` | Meaning |
| --- | --- |
| `BOAT_API_KEY` | An API key from Boat's dashboard (`boat_...`, a secret). Required. |
| `BOAT_API_URL` | Boat's API; default `https://boat.dev/api/v1`. Changing it needs the key typed again. |
| `BOAT_ORG` | Bill an organization's wallet instead of yours (its id or name). |
| `BOAT_MAX_TIMEOUT` | Seconds a sandbox runs before Boat stops it; default 30 days. The keeper renews it while the sandbox is used. |
| `BOAT_MAX_CPU`, `BOAT_MAX_MEMORY_MB`, `BOAT_MAX_DISK_MB` | Per sandbox; default 8 vCPUs, 16384 MiB, 113664 MiB (the $20 plan's `large`). |
| `BOAT_MAX_SANDBOXES` | Running at once; default 100 ($20 plan). |

The web app offers Boat's paid plans as presets, from Boat's
[pricing](https://docs.boat.dev/pricing.md), [limits](https://docs.boat.dev/limits.md)
and [machines](https://docs.boat.dev/machines.md) as read on 2026-11-17:
$20 (up to `large`: 8 vCPUs, 16 GB, 111 GB; 100 at once), $100 (`xlarge`:
16 vCPUs, 32 GB, 251 GB; 300), $500 (1,000) and $2000 (2,000), each with
30-day auto-stop. Not the free trial: it can't save snapshots, which sbx's
templates are, so the $20 plan is the least sbx runs on. A sandbox gets the
smallest machine its size fits: `small` (2 vCPUs, 4 GB, 51 GB), `default`
(4, 8, 51), `large` (8, 16, 111) or `xlarge` (16, 32, 251). Boat's refusals
(sandboxes at once, starts per minute, a size the plan lacks, billing, the
trial) are said with what to change and shown on the runtime's row; its
"out of capacity" asks to try again. The status warns while the account is
on the trial and when the API key expires within 14 days.

How sbx runs on it:

- **Templates are saved snapshots.** A build replays the template's steps
  in a builder sandbox (from the parent template's saved snapshot, or Boat's
  own Ubuntu 24.04 for `ubuntu:24.04`; another image is refused), saves it
  under the template's name and deletes the builder. A Dockerfile's `RUN`,
  `ENV`, `WORKDIR`, `USER` and `ARG` replay; `COPY` and `ADD` are refused
  (fetch the file in a `RUN`). The start command becomes an enabled systemd
  unit, which Boat starts again on every resume.
- **Pause is Boat's stop, and every resume a cold boot.** Boat keeps the
  disk, not the memory: on resume sbx puts back what lives in `/run/sbx`
  (env, keys, the guest helper, endpoint certificates) and restarts the
  recipe's processes, whatever `lifecycle.resume` says. A sandbox stopped
  for more than 14 days may have lost its disk (Boat's retention); its
  template starts a new one.
- **Previews** go to `https://<subdomain>-<port>.on.boat.dev`. Each port
  answers only with its own token: the first request for a port exposes it
  (private, never public; through Boat's relay when the app listens on
  loopback only), sbx keeps the token until the sandbox stops, and nginx
  appends it to the request's cookies as `_port_auth`, after the app's own,
  for Boat's sandboxes only (a browser's own `_port_auth` is dropped). Boat
  reserves ports 22, 6080, 8090, 8911 and 47984-48010: never previews.
- **The desktop** is Boat's own: the `-desktop` preview opens a fresh
  Boat desktop URL (valid 10 minutes to open). sbx starts no noVNC there.
- Sandboxes are created with none of the Boat account's secrets (`noEnv`)
  and labelled with the installation (`sbx_install`) and their template
  (`sbx_template`); sbx lists by label, so installations sharing a key keep
  to their own. Boat keeps at most 20 labels and 100 env variables (64 KB)
  per sandbox.
- No per-sandbox egress rules (`egress_rules` false): untrusted public repos
  are refused while Boat is enabled. No route to this host's network
  (`private_networks` false): managed servers through [endpoints](#endpoints).

### Enabling runtimes

The runtimes are set in `/etc/sbx/runtime.env` (root only, not in backups):

| Key | Meaning |
| --- | --- |
| `SBX_RUNTIMES` | The enabled runtimes, by kind, in order: `SBX_RUNTIMES=embed`. Missing means `embed` (as before several runtimes); present and empty (`SBX_RUNTIMES=''`) means none yet. |
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
  again, it replaces the key. It then starts the base and default template
  builds on that runtime in a process of their own (a transient systemd
  unit, `sbx-template-builds-<kind>`) and returns; `sbx-admin runtime
  verify KIND` works once they are built, and says so while the base is
  still building. Embed is the installer's: it needs the host set
  up.
- `sbx-admin runtime remove KIND` disables one, refused while it has
  sandboxes (running or paused) or when it is the only one. A provider's
  keys leave the file.
- `sbx-admin runtime default KIND` sets `SBX_RUNTIME_DEFAULT`.

Each then does what `runtime apply` does.

### From the web app

Admins add, change and remove runtimes and choose the default in Settings →
Sandbox runtimes, and the first-run wizard adds the first one. A runtime on
this server (Embed) is `install.sh`'s: the web app shows its host facts and
says so, with no edit or remove. sbx-control checks each change, then queues
it for the root side; it never edits `runtime.env` itself:

1. The request is checked with no root: the kind exists and is a provider's,
   the values fit the runtime's settings (an int is a whole number, at
   least 1), the provider takes them (the backend's `configure` and its own
   checks, as `runtime add` does), a provider's runtime has a domain to call
   back, no sandbox is left on a runtime being removed (409, naming them),
   the last runtime stays. Refusals are 400 or 409 with `detail`; nothing is
   queued.
2. A secret goes into the managed store (Infisical) under a new reference.
   **Adding or changing a runtime from the web app needs the Infisical
   store**: on an installation whose store is still the local one, those
   requests answer 409 ("set up the secret store first, or use `sbx-admin
   runtime add KIND` on the server") and the web app shows that instead of
   the form. Removing a runtime and choosing the default carry no secret and
   work either way.
3. The change is written to `/run/sbx/apply/runtime-change` (root only): the
   operation, the kind and the values, a secret only as its reference. Its
   path unit runs `sbx-apply runtime-change`, which is `sbx-admin runtime
   apply-pending`: the same change as `sbx-admin runtime add | remove |
   default`, checked the way sbx-control reads it, put back on any failure
   (and the queued reference discarded), then nginx regenerated and
   sbx-control restarted. Its outcome is kept in
   `/var/lib/sbx/runtime-change.json`. A request the root side has not taken
   within two minutes fails ("the root side did not pick up the change:
   check sbx-apply-runtime-change.path"), and goes with its reference. A
   change made but whose old store reference could not be discarded is
   done, saying so.
4. `GET /api/runtimes/change` answers `{state, operation, kind, message, at,
   restarting}`, `state` one of `idle`, `queued`, `applying`, `done`,
   `failed`; `restarting` is true while a done change is answered by the
   sbx-control from before its restart. The web app polls it every 2 seconds
   until the change failed, or is done and a restarted sbx-control answers,
   then refreshes the runtimes. One change at a time: another request while
   one is queued or applying is a 409, and `sbx-admin runtime add | remove |
   default` take the same lock as the root side.

A secret travels from the browser over HTTPS to sbx-control, which checks it
with the provider in memory and seals it into Infisical; from there on only
the reference moves (the request file, `runtime.env`). No log, request file
or API answer holds its value, and the web app shows a set secret only as
"set" (`secrets_set`), replaced only when a new one is typed.

| Route | What |
| --- | --- |
| `GET /api/runtimes/available` | Every runtime an installed package provides: `kind`, `title`, `host`, `enabled`, `default`, `can_run` and `why_not` (Embed: `/dev/kvm`, memory, disk, as `install.sh` checks), `managed_by` (`install.sh` for Embed), `not_editable` (why the web app can't add or change it), `settings` (`key`, `label`, `secret`, `default`, `help`, `kind`: `text` or `int`, `optional`, `limit`), `presets` (`name`, `title`, `values`, `source`), `values` (the non-secret ones now), `secrets_set`, `facts` (Embed's host facts), `sandboxes` (an enabled one's, by name), `removable` (why not: `""` when it can be), `limit_hit`. |
| `POST /api/runtimes` | `{"kind": "e2b-cloud", "values": {"E2B_CLOUD_API_KEY": "...", ...}}`: add a provider's runtime. 202 with the change's state. |
| `PUT /api/runtimes/KIND` | `{"values": {...}}`: change an enabled provider's settings; a secret left out or empty keeps its value, another empty value goes back to its default. |
| `DELETE /api/runtimes/KIND` | Remove it: 409 while sandboxes run on it (named) or when it is the last one. |
| `PUT /api/runtimes/default` | `{"kind": "KIND"}`: new sandboxes run on it unless the request or the repository names another. |
| `GET /api/runtimes/change` | The last change, as above. |
| `POST /api/runtimes/KIND/test` | An enabled runtime's checks: `{kind, ok, checks: [{name, ok, detail, fix, runtime}]}`: its own (the provider answers with the stored key), its limits, its base template (built, building, or failed with how to rebuild) and, once that is built and the rest pass, whether its sandboxes reach this server. 400 for a runtime not enabled. |

All of them are for admins, and for the first-run setup link's token while
the setup is open. `settings` also carry an int's `min`, `max`, `step` and
`unit` (`seconds`, `MiB`, or `""` for a count), `presets` their
`adjustable` keys and `note`, and an enabled runtime its `plan` (`{name,
title}`, `custom` when no plan matches; also in `GET /api/runtimes`).

The same from a laptop, for an admin or a coding agent, with the `sbx` CLI
(over this API, as the signed-in admin; the API token's email must be an
admin):

```sh
sbx runtimes                          # the enabled ones, each with its plan
sbx runtimes available [--json]       # what can run here and why not, the plans, each setting now
sbx runtimes add e2b-cloud --plan hobby --secret E2B_CLOUD_API_KEY < key-file
sbx runtimes add e2b-cloud --plan pro --set E2B_CLOUD_MAX_CPU=16 --secret E2B_CLOUD_API_KEY < key-file
sbx runtimes set e2b-cloud --plan custom --set E2B_CLOUD_MAX_SANDBOXES=50
sbx runtimes set e2b-cloud --secret E2B_CLOUD_API_KEY < new-key-file
sbx runtimes default e2b-cloud
sbx runtimes remove e2b-cloud
sbx runtimes test e2b-cloud [--json]  # its checks: ok / NO with the detail and fix; exit 1 when one fails
```

- `--plan` takes a plan's values from `sbx runtimes available` (`hobby`,
  `pro`) or `custom`; a plan fixes its limits but those it leaves adjustable
  (Pro's `E2B_CLOUD_MAX_CPU` and `E2B_CLOUD_MAX_MEMORY_MB`), so `--set` on a
  fixed one is refused before anything is sent. `--set KEY=VALUE` is in the
  setting's unit (seconds, MiB) and repeatable. `add` without `--plan` is on
  the first plan (Hobby), as the web form starts; `set --plan pro` keeps the
  runtime's vCPUs and memory now (Pro leaves them adjustable). `set` without `--plan` changes
  only what it names.
- A secret is never on the command line: `--secret KEY` reads its value from
  stdin, one line per `--secret` in order (hidden at a terminal); `--set` on
  a secret is refused.
- Each change waits until it failed, or is done and the restarted sbx-control
  answers (it rides out the restart's 502s), prints the outcome and exits
  non-zero when it failed; `--no-wait` returns once it is queued. Only a
  restart's answers (502, 503, 504, a refused connection) are waited out; any
  other refusal prints the API's `detail` and exits non-zero. `--json` prints
  the change's outcome (or, with `--no-wait`, the queued change) as the API
  answers it.
- A value that is itself a reference into the secret store
  (`infisical:...`) is refused (400): the API takes values, so no caller can
  have another secret of the installation unsealed and sent to a provider.
- `PUT /api/runtimes/default` for the runtime that is the default already
  answers 200, done, without queueing or restarting anything. A runtime whose
  key is missing (or whose reference no longer resolves) can still be
  removed: it lists no sandboxes, and the removal says so.

### The first-run setup without a browser

On a fresh server (one without `/dev/kvm` installs with no runtime, and the
setup can't finish without one, so `sbx login` can't sign in yet), an agent
takes every step of the wizard with `sbx setup`, as the holder of the setup
link install.sh printed. The link is a credential: it goes in
`SBX_SETUP_LINK` or a file named by `--link-file`, never on the command line,
and stdin is left to the secrets (one line per secret, in the order asked).
`--insecure` accepts the self-signed certificate the server answers with
before it has its own.

```sh
export SBX_SETUP_LINK="$(cat link-file)"       # https://SERVER/setup#t=TOKEN
sbx setup status [--json]                      # each step, done or to do
sbx setup address --domain sbx.example.com --email ops@example.com   # or: --ip; waits for HTTPS
sbx setup runtime available
sbx setup runtime add e2b-cloud --plan hobby --secret E2B_CLOUD_API_KEY < key-file
sbx setup admin ops@example.com < password-file
sbx setup finish --admin ops@example.com < password-file
```

Each uses `Authorization: Setup <token>`. `address --domain` takes the DNS
provider's credentials with `--secret` (`cloudflare_api_token`, ...) and waits
until the certificate works; `runtime add` takes the flags of `sbx runtimes
add` and waits through the restart; `finish --admin` signs in with the
admin's password (`POST /api/setup/complete` with `{"email", "password"}`,
for a client without a browser session). The link stops working once the
setup is finished; then `sbx login`.

### No runtime yet

An installation may enable no runtime (`SBX_RUNTIMES=''`, from `install.sh`
on a server without `/dev/kvm` and without a provider's key). sbx-control
runs: `GET /api/runtimes` answers `{"default": "", "runtimes": []}`, creating
a sandbox is refused with "no sandbox runtime is set up: add one in Settings →
Sandbox runtimes", the template scheduler, the keeper and the gate have
nothing to do, and `sbx-admin doctor` reports it. The first-run wizard's
"Sandbox runtime" step lists the runtimes this server can run (Embed as
"Can't run here: needs /dev/kvm ..." when it can't) and finishing the setup
needs one; `sbx-admin setup-infer` does not count such an installation as
set up. `sbx-admin runtime remove` still refuses to remove the last runtime.

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
before proxying. It does not rewrite the app: use `${SBX_URL_*}` for origins and
callbacks. Browser cookies are per host, not per port, so on an IP-only server
give the app a cookie prefix with `${SBX_NAME}` to keep simultaneous sandboxes'
logins apart; `sbx_session` is reserved.

### When the app can't answer

A preview link always shows a page, never a bare error. Signed out, every
preview host goes to the login (also for names that don't exist), then back
to the link. Signed in, whatever keeps the app from answering is one small
page, light or dark as the browser prefers:

| State | Page | Status for non-HTML |
| --- | --- | --- |
| Being created, setting up, restarting, the app starting | "Starting memos-a…" and the step (Creating the sandbox, Running setup, Starting the app) | 503, `Retry-After: 2` |
| Paused or stopped | "Starting memos-a…", Waking up: the first request starts the resume | 503, `Retry-After: 2` |
| Nothing listens on the port | "Nothing is listening on port N in memos-a" | 503, `Retry-After: 2` |
| The app's processes exited | "memos-a's app stopped", Restart for its owner | 502 |
| The sandbox failed | "memos-a failed", what happened and Restart or delete | 502 |
| Deleted | "memos-a was deleted" | 404 |
| No such name | "No sandbox named memos-x" | 404 |
| Someone else's owner-only preview | "You don't have access" | 403 |

A waiting page polls `/.sbx/status` on the preview host (sbx's state, not
the app), backing off to every 5 seconds with no time cap. Once the app's
port answers it says Ready and replaces itself with the exact URL asked for
(path, query and hash; Back does not return to it). A request that waits
for a resume waits up to 2.5 seconds, then gets the page: nginx never hangs
on it. An upstream 502 or 504 (the app not listening yet) is the page too;
the app's own 401, 403 and other errors reach the browser as they are. A
non-HTML request (XHR, websocket, API client) gets the status and a JSON
body (`{"error": "<state>", ...}`), never the page. `/.sbx/status` is the
one path sbx reserves on preview hosts.

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
| `GET /api/runtimes/available`, `GET /api/runtimes/change`, `POST /api/runtimes`, `PUT /api/runtimes/KIND`, `DELETE /api/runtimes/KIND`, `PUT /api/runtimes/default`, `POST /api/runtimes/KIND/test` | admin, and the first-run setup link | `sbx runtimes available | add | set | remove | default | test`: add, change, remove a runtime, choose the default, run its checks; changes are checked, then applied by the host ([from the web app](#from-the-web-app)). |
| `GET /api/endpoints` | admin | `sbx endpoints`: `{"endpoints": [{"name", "address", "kind": "managed-server" or "internal", "server", "server_kind", "target", "repos", "state": "on" or "applying" or "failed", "message", "used_by"}], "candidates": [{"server", "kind", "address", "target"}], "domain", "needed", "applying", "failure"}`. `repos`: who may use it (an internal host's grants; the repositories attached to a managed server); `used_by`: the repositories whose sandboxes hold its certificate, for which `DELETE` is refused; candidates are the managed servers without an endpoint; `needed` says an enabled runtime's sandboxes don't reach this host's private network; `applying` that a change is not applied yet, `failure` why the last one failed ([endpoints](#endpoints)). |
| `POST /api/endpoints`, `PUT /api/endpoints/NAME`, `DELETE /api/endpoints/NAME` | admin | `sbx endpoints add | grant | revoke | rm`: `POST` body `{"server": "NAME"}` (optionally with `"name"`) or `{"name": "NAME", "target": "HOST:PORT", "repos": ["OWNER/REPO"]}`; 202 with the endpoint, `applying`. 400 for an invalid name, target or repository, 409 for a name or target taken or no domain. `PUT` `{"repos": [...]}` replaces an internal host's repositories (400 for a managed server's: attach the server instead). `DELETE` is 409 naming the repositories while their sandboxes on a provider runtime use it. |
| `POST /api/sandbox/endpoint-certs` | a sandbox (`Bearer NAME:CREDENTIAL`, its own credential) | The forwarder's renewal: new certificates and keys for the endpoints the sandbox's repository may use now; 401 for a wrong credential, 404 for a sandbox that is gone, 403 when it may use none. Not cached. |
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
