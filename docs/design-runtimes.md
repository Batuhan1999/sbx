# Design: several sandbox runtimes per installation

Status: phase 1 of [sbx#18](https://github.com/Batuhan1999/sbx/issues/18). This
file is the contract the backend, the web app and the docs build against; it
is removed or folded into `docs/development.md` once phase 1 has landed.

## Goal

An installation enables every runtime it can: Embed (Firecracker on this
host) only where the server can run it, and any third-party provider once its
credentials are configured. Each sandbox runs on exactly one runtime, chosen
when it is created, and keeps it until it is deleted. The core never names a
runtime; it asks the registry.

## Decisions

1. **Configuration lives in `/etc/sbx/runtime.env`** (root-only, not backed
   up, as today):
   - `SBX_RUNTIMES=embed` lists the enabled runtimes in order. Missing
     means `embed`, so every existing installation keeps working unchanged;
     present and empty means none yet (phase 3: added from the web app).
   - `SBX_RUNTIME_DEFAULT=<kind>` picks the default for new sandboxes; missing
     means the first in `SBX_RUNTIMES`.
   - Each backend reads only its own keys (`Runtime.credential_keys`). Two
     backends never share a key name: the E2B Cloud backend reads
     `E2B_CLOUD_API_KEY`, never Embed's `E2B_API_KEY`.
2. **A registry like the connectors' one.** Built in:
   `{"embed": "sbx_control.runtime.embed:Embed"}`. Installed packages add
   backends through the entry-point group `sbx.runtimes` (name = kind, value =
   `module:Class`). An enabled kind that no package provides fails at start
   with the kinds that exist.
3. **No process-global configuration.** A backend must not keep its
   credentials where another backend reads them (the E2B SDK reads `E2B_*`
   from the process environment). Since phase 2 both E2B-based backends
   (Embed, E2B Cloud) pass their own connection on every SDK call through the
   shared adapter (`runtime/e2b/`) and never write the environment. The rule
   is written in `runtime/base.py`.
4. **Each sandbox records its runtime.** `Info.runtime` (the kind) is set by
   the registry when it lists; sbx's `sandboxes` table gets a `runtime` column
   written at create (existing rows: the installation's default at upgrade).
   `runtime.of(sandbox_id)` finds the backend: from an index the listings keep
   fresh, else by asking each enabled runtime's `info()`; unknown raises
   `Failed`.
5. **Module API** (`sbx_control.runtime`), replacing `current()`:
   - `kinds()` (available), `enabled()`, `default()`, `get(kind)`,
     `of(sandbox_id)`.
   - Per-sandbox calls route through `of()`: `connect`, `info`, `kill`,
     `pause`, `set_timeout`, `snapshot`, `upstream_host`, `snapshot_usage`.
   - `create(..., kind=None)`, `build_template(..., kind)`,
     `templates(kind)`, `delete_template(name, kind)`: `None` = the default.
   - `sandboxes(...)` lists every enabled runtime and tags each `Info`.
   - Host-side calls (`host_units`, `host_commands`, `guard_host`,
     `apply_host`, `apply_network`, `disk`, `free_disk_mb`,
     `collect_storage`, `jobs`, `capacity`, `placing`, `readiness`, `status`,
     `test`) run over the enabled runtimes that have a host side.
   - `capabilities(kind=None)`, `title(kind=None)`.
6. **The contract has two parts.** Every backend implements the compute part
   of `Runtime`. The host part has no-op defaults in a base class
   (`runtime.base.HostSide`) that hosted backends inherit, so they implement
   nothing they do not have. Embed overrides it.
7. **Choosing the runtime of a new sandbox**, in order: the request's
   `runtime` (API field, `sbx up --runtime`), the repo's setting (admins:
   `PUT /api/repos/{repo}/runtime`), the installation's default. sbx then
   checks the choice against what the recipe needs, and refuses with the
   reason instead of picking another runtime:
   - the runtime is enabled (a repo setting naming one that no longer is
     is refused, naming the setting, never replaced by the default);
   - managed servers attached to the repo need `capabilities.private_networks`;
   - the recipe's resources fit the runtime's limits (from #29).
8. **Templates are built per runtime.** The template-builds table is keyed by
   `(runtime, key)`; existing rows belong to the installation's default at
   upgrade. The scheduler builds the base and default templates on every
   enabled runtime, and a repo's own template on the runtimes its sandboxes
   use (its setting, the default, and any runtime a sandbox of it runs on).
9. **Previews pick the sandbox's runtime.** The gate returns the sandbox's
   runtime kind (`X-Sbx-Runtime`) next to `X-Sbx-Upstream`; nginx has one
   `upstream` block per enabled runtime and a `map` from the kind to it, and
   clears every enabled runtime's `ingress_cleared` headers.
10. **A fake runtime and a verify command.** `runtime/fake/` is an in-memory
    backend for the test suite (and the conformance tests every backend must
    pass). `sbx-admin runtime list` shows the enabled runtimes;
    `sbx-admin runtime verify [kind]` creates a sandbox from the base
    template, runs a command, writes and reads a file, pauses and resumes it,
    reaches a preview port through the gate, and deletes it, reporting each
    step.
11. **Switching a sandbox between runtimes is out of scope**: delete and
    create again.

## API contract (for the web app and the CLI)

- `GET /api/runtimes` (signed in):
  `{"default": "embed", "runtimes": [{"kind": "embed", "title": "E2B Embed",
  "default": true, "host": true, "capabilities": {...}, "limits": {...},
  "status": {"state": "connected", "summary": "..."}}]}`.
  `host` says it runs on this server. `limits` is #29's shape (or `{}`).
- Sandbox payloads (`GET /api/sandboxes`, `GET /api/sandboxes/{name}`,
  `/api/sandbox-list`) gain `"runtime": "<kind>"`.
- `POST /api/sandboxes` accepts an optional `"runtime": "<kind>"`. A refusal
  is a 400 whose `detail` names the runtime and the reason.
- `GET /api/repos/{repo}` gains `"runtime": "<kind>" | null` (its setting);
  `PUT /api/repos/{repo}/runtime` with `{"runtime": "<kind>" | null}` (admins)
  sets or clears it.
- CLI: `sbx up NAME --runtime KIND`; `sbx runtimes` lists them.

## Out of phase 1

The E2B Cloud backend (phase 2), adding runtimes from the web app and their
settings forms (phase 3), shared services over TLS subdomains (phase 4), the
boat.dev backend (phase 5).
