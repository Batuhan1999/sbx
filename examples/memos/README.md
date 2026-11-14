# Memos

A recipe for [Memos](https://github.com/usememos/memos): the Go server with its
embedded React frontend, built from source, using its normal SQLite storage and
local attachments. It needs no shared server or external account. It was written
against upstream commit `b40d8a7b573774c52b022c8d6537d6baf2129dc0`.

## The recipe

| File | Role |
| --- | --- |
| `.sbx/sbx.yaml` | One preview on port 5230, the `memos` process, a health check, a generated `MEMOS_ADMIN_PASSWORD`, and the resource request. |
| `.sbx/setup` | Installs the Node and Go versions the repository asks for (checksum-verified, via `toolchains.py`) and its pinned pnpm, then runs `pnpm release` and builds `./cmd/memos`. |
| `.sbx/config/users.json` | Declares the `sbx-admin` user, whose password is read from the injected env. |
| `.sbx/verify.py` | Signs in, writes and reads a private note, uploads an attachment and checks its bytes, and checks that signed-out readers see neither. `--seed` writes, without it reads back. Prints no credentials. |

## Run it

Either fork Memos, copy this `.sbx/` folder into the fork's root, push it, and
connect the fork in sbx; or, as an admin, apply the recipe to the upstream
repository without changing it:

```sh
sbx recipe apply /path/to/examples/memos/.sbx --repo usememos/memos
sbx template build --repo usememos/memos --wait   # the recipe sets template:
sbx up memos-a --repo usememos/memos              # prints the preview link when healthy
```

Sign in as `sbx-admin` with the sandbox's generated `MEMOS_ADMIN_PASSWORD`. To
run the functional check:

```sh
echo 'python3 /run/sbx/app/verify.py --expect-empty --seed' | sbx sandbox exec memos-a
echo 'python3 /run/sbx/app/verify.py' | sbx sandbox exec memos-a   # after restart or resume
```

Notes and data live on the sandbox's disk and are deleted with it; this is not a
backup. Optional storage backends, SSO and AI providers are not configured.

## How this recipe was made

Model for a single-binary app whose state is SQLite and files on disk: no
`services:`, nothing to clean up outside the VM. Recipe: `examples/memos/.sbx/`.

**What the repository showed.** `go.mod` names the Go version and `cmd/memos` is
the server. The frontend lives in `web/`; `web/package.json` pins pnpm in
`packageManager` and has a `release` script that builds the UI into the server's
embedded assets, so one `go build` after it yields the whole app. Memos stores
its SQLite database and attachments in a data directory, and its declarative
configuration (`--config-dir`) can create users, reading values from env.
Upstream's [build instructions](https://usememos.com/docs/development/building)
matched what the files said.

**Decisions.**

- Storage: the sandbox filesystem, as the service selection guide (`docs/service-selection.md`)
  says for SQLite and uploads. `MEMOS_DATA` points outside the checkout, so
  source operations never touch data. Each sandbox is independent by
  construction.
- Login: `users.json` declares an admin whose password is `${env:MEMOS_ADMIN_PASSWORD}`,
  and the recipe generates that value with `${random:32}`. No default password
  exists at any point; nothing is declared under `secrets:` because nothing
  needs the owner.
- Toolchains: setup reads the versions from `go.mod` and `web/package.json` and
  installs them under `~/.local`, checking upstream release checksums, instead
  of relying on whatever the template happens to carry.
- Size: 2 vCPU and 4 GiB (the default) fit once compilation is limited:
  `go build -p 2` with `GOMAXPROCS=2` and `GOMEMLIMIT=2GiB`. `swap_mb: 1024`
  is set explicitly, since swap takes disk that toolchains and build caches
  need. See the resource guide.
- Template vs setup: `template:` is set but there is no `.sbx/build`, so the
  template carries only the checkout and setup does every install. Setup skips
  toolchains and pnpm when the right versions are present, so reruns are cheap.
  Moving those installs into `.sbx/build` is the next step if launch time
  matters.
- Process and health: the built binary on port 5230 (the port Memos
  documents for its container image), with `/` as the health URL; the server
  serves the UI itself, so a 200 there means the build and the server both work.

**Pitfalls.**

- A Corepack pnpm shim outside `web/` picked a different pnpm version than the
  frontend pins. Setup installs the pinned pnpm into `~/.local` and calls that
  executable by path. Do this for any repository that pins a package manager.
- A health check proves the server answers, not that notes persist. The
  functional check uses the real API: create and edit a private note, upload an
  attachment, compare its bytes, and confirm signed-out requests are refused.

**How it was verified.** The acceptance checklist (`docs/agent-install.md`)
with two sandboxes: `verify.py --expect-empty --seed` on each showed that the
second started without the first one's note and stored its own. A session token
from one sandbox was rejected by the other. `verify.py` without `--seed` passed
after `sbx sandbox prepare`, a restart, and pause/resume, including the exact
attachment bytes. Deleting the disposable sandbox left nothing behind, since all
state was on its disk. Not verified: optional storage backends, SSO, AI providers.
