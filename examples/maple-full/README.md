# Maple

A recipe for the full [Maple](https://github.com/MapleTechLabs/maple) source
application in one sandbox: the Vite web app, Maple's local Alchemy development
services, the Rust ingest gateway, the Go OpenTelemetry collector and the
scraper. Postgres and ClickHouse are shared servers; each sandbox gets its own
databases, logins and Electric sync source. It was written against upstream
commit `e6f7b56724323f1ff3a4e0adb43862ffe2dbd406` on x86_64. This is development
hosting, not a production Maple deployment.

## Prerequisites

A Postgres and a ClickHouse connection, which the recipe names `postgres` and
`clickhouse` (`connections:` in `.sbx/sbx.yaml`; change them to the keys of
connections you already have), and the `electric-source` package from this
folder. To run both servers on the host under those names:

```sh
sbx-admin server up postgres --kind postgres
sbx-admin server up clickhouse --kind clickhouse
sbx-admin network apply      # before creating sandboxes
sbx-admin catalog install /path/to/examples/maple-full/electric-source
```

`sbx catalog push examples/maple-full/electric-source` installs the package from
a workstation instead.

## The recipe

| File | Role |
| --- | --- |
| `.sbx/sbx.yaml` | Services `db` (`postgres-clone` with replication), `warehouse` (`clickhouse-database`) and `sync` (`electric-source`); generated keys and passwords; five processes and their health checks. |
| `.sbx/setup` | Installs the runtimes Maple asks for, builds native parts, writes the runtime env, and applies the Postgres and ClickHouse schemas. |
| `.sbx/configure.py` | Writes `.env.local` (only when it changes) and the collector's config from the injected env. |
| `.sbx/stack`, `.sbx/docker` | Run Maple's Alchemy development services; Docker runs inside the VM, never on the host's socket. |
| `.sbx/vite.config.mts` | Wraps the web app's Vite config with same-origin proxies and the preview host. |
| `.sbx/verify-full.py`, `.sbx/verify-sync` | Check sign-in, ingest, the warehouse and Electric sync; print no keys. |
| `electric-source/` | A catalog package: a private Electric source per sandbox, using that sandbox's database and secret. |

## Run it

Fork Maple, copy this `.sbx/` folder into the fork, push it and connect the fork;
or, as an admin, apply the recipe to the upstream repository:

```sh
sbx recipe apply /path/to/examples/maple-full/.sbx --repo mapletechlabs/maple
sbx template build --repo mapletechlabs/maple --wait   # the recipe sets template:
```

Then:

```sh
sbx up maple-a --repo mapletechlabs/maple
sbx sandbox exec maple-a <<'SH'
python3 /run/sbx/app/verify-full.py --expect-empty --seed
bash /run/sbx/app/verify-sync
SH
```

Open the preview link and sign in with the sandbox's generated
`MAPLE_ROOT_PASSWORD`; check the rendered app, not only the status code. After
editing the recipe, apply it again and run `sbx sandbox prepare NAME` to
rerun setup without losing source edits. A change to an existing service's
configuration needs a new sandbox. Deleting a sandbox removes its databases and
Electric source and keeps the shared servers.

## How this recipe was made

Model for a multi-process stack with native builds, two shared servers, and a
sidecar that needs a package of its own. Recipe: [`examples/maple-full/.sbx/`](.sbx/);
package: [`examples/maple-full/electric-source/`](electric-source/).

**What the repository showed.** `package.json` pins Bun (`packageManager`),
`mise.toml` pins Rust, `otel/builder-config.yaml` defines the custom collector;
`apps/ingest` is Rust, `apps/scraper` Bun, `apps/web` Vite. Developers start the
API and workers with `scripts/dev.ts` through Alchemy, whose local emulation
needs Docker and the `portless` proxy for `*.localhost` names. `packages/db`
holds the Postgres migrations, `packages/clickhouse-cli` the warehouse schema.
The self-hosted mode signs in with a root password and talks to plain
ClickHouse; Electric syncs Postgres tables to the browser. The README did not
reveal the Docker and native-build needs; the dev script and manifests did.

**Decisions.**

- Services: `postgres-clone` from `template0` with `replication: 'true'`
  (Electric streams the database) and `createrole: 'true'` (migrations that
  create roles); `clickhouse-database` with empty schemas that setup fills with
  `clickhouse-cli apply`. No catalog package ran Electric, so `electric-source`
  starts one container per sandbox on the service network with a per-sandbox
  secret, and removes it on delete (package format: `docs/extensions.md`).
- Values: `${random:32}` for the root password, `${random:base64:32}` for the
  ingest-key encryption key (it must decode to exactly 32 bytes), `${random:hex64}`
  for HMAC keys and internal tokens; the rest are service outputs or fixed
  settings. `TINYBIRD_*` point at the same ClickHouse database.
- Processes: Alchemy's workers (`.sbx/stack`), collector, ingest, scraper and
  web, each with a health URL. The collector listens on loopback only; ingest
  forwards to it, and it is not previewed.
- Size: 4 vCPU, 8 GiB, 2 GiB swap; `cargo build -j 2`, `GOMAXPROCS=2` and
  `GOMEMLIMIT=2GiB` keep native builds inside that, and setup runs
  `go clean -cache` below 1 GiB free disk. Swap shares that disk, so set it.
- Template vs setup: no `.sbx/build` yet; setup installs everything and skips
  what is current. Cold launches still compile Rust and Go for minutes:
  runtimes, `node_modules` and native binaries belong in `.sbx/build`, never
  `.env.local` or data.

**Pitfalls.**

- The browser cannot reach the VM's `localhost` ports. `.sbx/vite.config.mts`
  serves API, sync and ingest as `/_api`, `/_sync`, `/_ingest` on the preview
  origin, keeping tokens server-side.
- A `.ts` Vite config outside the package loaded as CommonJS and rejected an
  ESM-only plugin; `.mts` fixes it ([Vite](https://vite.dev/guide/troubleshooting.html#this-package-is-esm-only)).
- Vite rejects unknown `Host` headers. A preview reaches the app with the
  runtime's own host as `Host` (the browser's is in `X-Forwarded-Host`), and
  sbx injects that host's suffix as `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS`;
  a config that sets `server.allowedHosts` itself, as this one does, lists
  that suffix too (`.e2b.sandboxes.internal` here; `.on.boat.dev` on Boat).
- Two Electric sources collided on the default replication slot; the package
  uses temporary, randomly named slots
  ([Electric](https://electric.ax/docs/sync/api/config#electric-temporary-replication-slot-use-random-name)).
- The ingest gateway's default queue directory was not writable;
  `INGEST_QUEUE_DIR` moves it under the home directory.
- Rewriting an unchanged `.env.local` restarted Vite; `.sbx/configure.py`
  writes it only when the content changes.
- Telemetry and session capture are off (`TURBO_TELEMETRY_DISABLED`,
  `ELECTRIC_TELEMETRY`, the web app's replay and capture flags).

**How it was verified.** The acceptance checklist (`docs/agent-install.md`)
with two sandboxes. `.sbx/verify-full.py --expect-empty --seed` signs in, issues
an ingest key, sends a span (refused without the key) and reads it back from
that sandbox's ClickHouse; `.sbx/verify-sync` checks that a Postgres insert and
update reach the authenticated sync proxy and that Electric refuses requests
without its secret. The second sandbox's Postgres and ClickHouse logins were
denied on the first one's databases. Prepare, restart and pause/resume kept the
span, the login and live sync. Deleting the disposable sandbox removed its
databases, logins, Electric container and replication slot. Not verified: LLM,
billing, OAuth and other optional providers.
