# Resource guide

How to choose CPU, RAM, disk, swap and build concurrency for a repository's
sandboxes. Size from measurements, not from guesses, and keep three facts apart.

## Three different facts

| Fact | Where it comes from | What it tells you |
| --- | --- | --- |
| **Request** | `resources:` (or `build_resources:`) in `.sbx/sbx.yaml` | The size of sandboxes started after the matching template is built. |
| **Allocation** | `allocated` in the resources snapshot | What the runtime actually gave this VM. |
| **Observed use** | `usage` in the snapshot; inside the VM `nproc`, `free -m`, `df -h /`, `df -i /` | What the workload consumes right now. |

Get the snapshot with `sbx sandbox resources NAME` (JSON with
`allocated`, `recipe`, `usage`, `host`, `issues` and an `agent_guidance`
summary), or with `GET /api/sandboxes/NAME/resources?probe=true`. Without
`probe=true` the API leaves out live `usage`. A paused sandbox reports its
allocation and the issue `usage_unavailable`; it is not resumed to measure it.

Never call a requested size allocated until the snapshot shows it.

## Where each request takes effect

```yaml
resources:
  cpu: 4
  memory_mb: 8192
  disk_mb: 8192
  swap_mb: 2048
```

- `cpu`, `memory_mb` and `disk_mb` are the size of every sandbox of the
  repository, with or without `template:`. The runtime sizes a VM by its
  template, so the platform builds a template at each size: the repository's
  own, or a default template of that size. Unset values default to 2 vCPU,
  4 GiB RAM and 8 GiB of free disk after the template build; `.sbx/setup`
  then uses some of it. Until the template of a new size is built,
  new sandboxes start from the repository's own earlier template, or are
  refused when it has none.
- `build_resources`, when present, replaces `resources` for `cpu`, `memory_mb`
  and `disk_mb`. It is the same size, not a separate build budget. The
  snapshot's `recipe.allocation_source` says which was used (`default` for
  neither).
- `disk_mb` asks the runtime for at least that much free disk after the template
  build. It is best effort, not a resize and not a total size. Check the actual
  writable space after the template build and again after `.sbx/setup`.
- `swap_mb` (256 to 65536) applies to every sandbox, with or without a
  template, each time it is prepared: the swap file is resized in place. A
  change is refused when the VM lacks the free RAM to do it safely; use a new
  sandbox then. Swap uses disk and is much slower than RAM; it does not replace
  enough memory. Without it a sandbox keeps its template's swap file (below).
- vCPUs share the cores of the machine the sandbox runs on; they are not
  pinned or dedicated.
- A recipe change never resizes an existing VM. The snapshot reports
  `allocation_below_recipe` until you build the template again and start a new
  sandbox. Keep source edits and data you need before replacing one.

## Each runtime differs

The same recipe can pass on one runtime and fail on another: a full
`yarn install` that fit on E2B Cloud was OOM-killed on Boat. Test the recipe
on every runtime the repository uses, and set what it depends on.

| | Default | Differs because | Set it |
| --- | --- | --- | --- |
| Swap | an 8 GiB swap file behind zswap (base template) | a runtime's image can bring its own (a Boat sandbox showed 2 GiB); set `swap_mb` to be sure | `resources.swap_mb` |
| Node | 22.22.2 in `/usr/local/bin` (base template) | a runtime's image can put another first on `PATH` (Boat's showed Node 24) | install the version the repository pins (`.nvmrc`, `engines`) in `.sbx/setup` or the template, and check `node -v` |
| Disk | at least `disk_mb` (8 GiB) free after the template build | the runtime gives more: E2B Cloud its plan's disk (Hobby 10 GiB, Pro 20 GiB), Boat its machine's (51 GB and up) | `resources.disk_mb`, then check `df -h /` |

Every template carries the base template's contents, which include Amp's CLI,
desktop tools and packages (about 0.6 to 0.7 GB) whether Amp is enabled or not.
On E2B Embed `disk_mb` is free space after the build, so the default 8 GiB still
holds; on a runtime with a fixed disk (an E2B Cloud plan, a Boat machine) that
space comes out of what the sandbox has free, so check `df -h /` there rather
than trusting `disk_mb`.

## Find the size

1. Start without a size. Run the app's real workload (setup, the start
   command, the useful action) and read the snapshot.
2. A command killed with exit code 137 (`killed (exit code 137: out of
   memory?)`) usually ran out of memory: `sudo dmesg | grep -i oom` in the
   sandbox names the process. If it is short of memory or CPU, first limit
   build and test parallelism
   (`CARGO_BUILD_JOBS`, `GOMAXPROCS`, `make -j`, test worker counts) in
   `.sbx/setup`. A smaller job count often fits where a bigger VM was assumed.
3. Only then request more. As a starting hypothesis, a small app runs in 2 vCPU and 4 GiB; a
   multi-process app with native compilation may need 4 vCPU and 8 to 16 GiB.
4. Build the template (`sbx template build`), start a new sandbox, and confirm
   the new allocation and the workload in the snapshot.

When the snapshot reports an issue:

| Issue | Do |
| --- | --- |
| `allocation_below_recipe` | Build the template and start a new sandbox; the old VM keeps its size. |
| `disk_request_not_applied` | Do not retry the same build. The request exceeds what the runtime gave this filesystem; lower it or check the runtime's disk limit. |
| `low_disk_space` (under 1 GiB writable) | Separate source and app data from disposable caches (package stores, compiler output, images) and clean only the caches. Never delete application data to make setup pass. |

## The host has two budgets

The hugepage pool counts only for sandboxes that run on this server (E2B
Embed). A runtime at a provider sets its own limits instead: `GET /api/runtimes`
lists each runtime's, and a recipe beyond them is refused with the limit it
exceeds. The host reserve counts either way.

Sandbox RAM on this server comes from the host's hugepage pool. Everything else runs in the
**host reserve** outside that pool (12 GiB by default): the control service,
the secret store, shared Postgres and ClickHouse servers, the OS and its file
cache. Free memory inside a sandbox is not available to shared services, and
the other way round.

- The snapshot's `host` field counts sandbox slots of this recipe's memory
  size in the hugepage pool (`slots_total`, `slots_free`) and shows the host
  reserve.
- Managed Postgres and ClickHouse servers are each capped at 4 GiB and 4 vCPU.
  The caps are limits, not reservations; budget them inside the host reserve.
- Managed Postgres keeps its data on a preallocated XFS image (32 GiB by
  default). Copy-on-write clones share blocks with their template at first and
  grow as sandboxes write; watch the free space.

A safe number of sandboxes does not follow from RAM alone: count CPU
concurrency, the host reserve, shared-service growth, disk space (each
template, the base's contents included, is stored on the host) and the disk
that paused sandboxes keep. Keep the number of simultaneous builds separate
from the number of idle development sandboxes.

## Templates as acceleration

A prebuilt template can carry runtimes, dependency stores, generated code and
compiled binaries, so that `.sbx/setup` has little left to do. Add one when
repeated setup dominates launch time, not before. Setup must still reconcile a
changed lockfile or source. A template never holds credentials, per-sandbox
values or databases. After adding or changing one, start two fresh sandboxes
and repeat the acceptance checks.
