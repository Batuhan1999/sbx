# Umami

A recipe for [Umami](https://github.com/umami-software/umami): the self-hosted
Next.js analytics app with its normal Postgres storage. Each sandbox gets its
own database and login on a shared Postgres server through `postgres-clone`.
It was written against upstream commit `559e3c607c23d2c72d9bd4f6481bda428255323c`.

## Prerequisites

A Postgres connection, which the recipe names `postgres`
(`services.db.connections.server` in `.sbx/sbx.yaml`; change it to the key of a
connection you already have). To run a server on the host under that name:

```sh
sbx-admin server up postgres --kind postgres
sbx-admin network apply      # before creating sandboxes
```

## The recipe

| File | Role |
| --- | --- |
| `.sbx/sbx.yaml` | The database service, generated `APP_SECRET` and `UMAMI_ADMIN_PASSWORD`, the `umami` process, a health check on `/login`, and `lifecycle: {resume: restart}`. |
| `.sbx/setup` | Installs a checksum-verified Node and the repository's pnpm, the locked dependencies, and runs `pnpm build` (Prisma generation and migrations included). |
| `.sbx/bootstrap.mjs` | Replaces only the fresh default admin password with the injected one; a changed password is never overwritten. |
| `.sbx/verify.py` | Signs in, creates a website, posts a pageview to `/api/send` and reads it back from the report, and checks that a signed-out report request fails. `--seed` writes, without it reads back. |

## Run it

Fork Umami, copy this `.sbx/` folder into the fork, push it and connect the fork;
or, as an admin, apply the recipe to the upstream repository:

```sh
sbx recipe apply /path/to/examples/umami/.sbx --repo umami-software/umami
sbx template build --repo umami-software/umami --wait   # the recipe sets template:
sbx up umami-a --repo umami-software/umami
echo 'python3 /run/sbx/app/verify.py --expect-empty --seed' | sbx sandbox exec umami-a
```

Sign in as `admin` with the sandbox's generated `UMAMI_ADMIN_PASSWORD`. Deleting
a sandbox removes its database and login and keeps the server. Umami's optional
Redis and ClickHouse modes are not configured.

## How this recipe was made

Model for a Node app on Postgres whose ORM migrates during the build, and for
any app that ships a default admin password. Recipe: [`examples/umami/.sbx/`](.sbx/).

**What the repository showed.** `package.json` pins pnpm under `engines.pnpm`;
its `build` script runs Prisma client generation and `prisma migrate deploy`
before building the tracker and the Next.js app, so the build needs a reachable
database. The Prisma configuration and the install docs name `DATABASE_URL`
and `APP_SECRET`; Redis and ClickHouse are optional modes the owner did not use.
The first migration creates an `admin` user with the well-known password
`umami`. Upstream's [installation guide](https://docs.umami.is/docs/install)
describes the same build and start.

**Decisions.**

- Database: `postgres-clone` from `template0`, so each sandbox starts from an
  empty database with a login that owns it, and Umami's own migrations build the
  schema (the service selection guide (`docs/service-selection.md`)).
  Services are provisioned before setup, so `pnpm build` can migrate.
- Values: `APP_SECRET` is `${random:hex64}` and the admin password `${random:32}`,
  both generated and stable per sandbox. Nothing needs the owner, so there is no
  `secrets:` list.
- Default admin: `bootstrap.mjs` runs after the build and swaps the password
  only while it is still `umami`; if someone changed it, setup stops instead of
  overwriting it. Repeated setup is a no-op.
- Telemetry: `DISABLE_TELEMETRY` and `NEXT_TELEMETRY_DISABLED` keep development
  sandboxes from reporting to upstream.
- Process and health: the normal `pnpm start`, bound to `0.0.0.0` on port 3000.
  Health is `/login`, a page Next.js renders only when the server is up.
- Size: 4 vCPU and 8 GiB, because the Next.js build runs with a 4 GiB Node heap
  (`--max-old-space-size=4096`); 1 GiB swap set explicitly. See the resource guide.
- Template vs setup: `template:` is set without `.sbx/build`; the template holds
  the checkout and setup installs and builds. Dependencies could move into
  `.sbx/build`; the database and the admin password never can.

**Pitfalls.**

- After a pause, the first request failed with a 500 because the database pool
  kept dead connections; the second worked. `lifecycle: {resume: restart}`
  restarts the app after resume, without rerunning setup or migrations.
- Umami ignores pageviews from bot user agents yet still answers 200, so a
  scripted check must send a browser user agent or the count never rises.
  `verify.py` does.
- A Corepack pnpm shim can select a different pnpm than the one
  `package.json` pins. Setup installs the pinned pnpm into `~/.local` and calls
  it by path.

**How it was verified.** The acceptance checklist (`docs/agent-install.md`)
with two sandboxes: `verify.py --expect-empty --seed` on each showed the second
started without the first one's website and recorded its own pageview. One
sandbox's session token was rejected by the other, and the second sandbox's
database login could not connect to the first one's database. `verify.py`
without `--seed` passed after a rebuild with `sbx sandbox prepare`, a restart,
and repeated pause/resume. Deleting the disposable sandbox dropped its database
and login; the other sandbox and the shared server kept working. Not verified:
Redis and ClickHouse modes.
