# Service selection

How to decide which services a repository needs and which catalog package
provides each one. This is the only matching guide; the package format is in
[extensions](extensions.md#catalog-packages) and the recipe syntax in the
[reference](reference.md#services).

## Start from evidence

Read what the repository already does: dependency manifests, Compose files, ORM
and migration configuration, `.env.example`, provider configuration, CI and
deployment workflows. Then look at the connections already saved in sbx and the
provider accounts the owner has authorized. Access to an account shows what is
possible, not what the repository uses.

Before changing anything, show the owner one table:

| Service | Evidence in the repository | Connection | Per-sandbox resource | Variables injected |
| --- | --- | --- | --- | --- |

List only services the app needs. Keep the hosting the repository already uses.
When a credential is missing, ask for it; a missing credential is not a reason to
switch backends. Ask about a choice only when it changes behavior, cost or the
product mode.

## Match the protocol and the workflow

| The repository uses | Packages | Each sandbox starts with |
| --- | --- | --- |
| Neon | `neon-project` + `neon-branch` | A branch of the selected development parent. Discover the actual branch, database and owner instead of assuming `main`. |
| PlanetScale Postgres | `planetscale-database` + `planetscale-branch` | An empty development branch and a migration-capable role; run the normal migrations and fixtures. |
| Any other Postgres | `postgres-server` + `postgres-clone` | A database copied from `template0` or a selected [development baseline](reference.md#development-baselines). |
| Tinybird | `tinybird-workspace` + `tinybird-branch` | A branch with the workspace's structure; data from the last partition only when selected. |
| ClickHouse | `clickhouse-server` + `clickhouse-database` | Empty schemas, or clones of supported local MergeTree tables. |
| Upstash Redis | `upstash-account` + `upstash-redis` | A separate Redis database; run the repository's seeds, if any. |
| Temporal | `temporal-server` + `temporal-namespace`, or the local dev server as a process | A namespace, or a development store of its own. |
| Redis with a native client | A `processes:` entry in the app sandbox | Its own data directory; enable persistence if the app needs it. |
| SQLite, uploads, local files | The sandbox filesystem | Files created by normal setup. |
| AWS SDKs or AWS secret loaders | `aws-role` + `aws-profile` | A named profile with renewable role credentials ([AWS role access](../catalog/aws-role/README.md)). |
| Something else | The provider's branch or namespace API, in a new package | State what it copies and how it is cleaned up. |

Protocols matter more than names:

- Tinybird's HTTP APIs are not native ClickHouse. Keep Tinybird SDKs, datafiles
  and endpoints when the repository uses them.
- An Upstash REST client cannot talk to a local Redis TCP server. Check queues,
  streams, Lua and resets, not only whether a URL connects.
- A shared Redis with key prefixes is independent only if every migration,
  reset, worker and queue honors the prefix. Otherwise give each sandbox its own
  database.
- The PlanetScale package targets Postgres. It does not support Vitess, backup
  restore, or copying production schema or data.

Hosted branches and databases can have quotas or costs. Choose a plan
explicitly; never fall back to a paid tier because free provisioning failed.

## Local services

A dependency that runs fine inside the app's VM (Redis, Temporal's dev server)
can be a recipe `processes:` entry with a health check and its data in the
sandbox. Shared host servers (`sbx-admin server up`) exist for Postgres and
ClickHouse only; create one only when it was chosen and no compatible connection
exists, and apply its network before creating sandboxes. A repository that uses
only hosted services needs neither.

## Credentials that are just variables

Keys for email, payments, LLMs, analytics or error reporting are ordinary
repository variables (`sbx env set`); they need no package. When the repository
loads them from a secret manager, keep the loader and connect its identity
instead (for AWS, `aws-role` + `aws-profile`).

## PlanetScale token access

A service token scoped to one database, with these grants, is enough for
`planetscale-branch` to create and delete sandbox branches and their roles:

```sh
pscale service-token add-access TOKEN_ID \
  read_database read_branch create_branch delete_branch connect_branch \
  --database DATABASE --org ORGANIZATION --format json
```

Enter the token as a connection value in sbx, never in a recipe or chat.
`create_branch_password` is an endpoint permission label, not a grant name.

After choosing, verify isolation with the
[acceptance checklist](agent-install.md#acceptance-checklist).
