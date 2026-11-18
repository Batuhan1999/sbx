# Amp

The Amp connector makes sbx a self-hosted orb provider for
[Amp](https://ampcode.com). Each Amp thread runs in an ordinary sbx sandbox: it
gets the same checkout, per-sandbox services, injected env, recipe setup, health
checks, previews, pause/resume and deletion as a sandbox started with `sbx up`.
The connector code is in `control/sbx_control/connectors/amp/`; how connectors
plug in is described in [extensions](extensions.md#agent-connectors).

## What the connector contributes

- **Template layer**, while Amp is enabled: the Amp CLI, the Wayland desktop
  tools taken from Amp's published image, and their packages. Enabling or
  disabling Amp changes the template fingerprint, so templates rebuild by
  themselves.
- **A host service**, `sbx-amp`, that runs Amp's outbound provider controller.
  Only outbound HTTPS/WSS to Amp is needed; no public port is opened.
- **A job route** on the control service's loopback interface. Amp's dispatch
  program posts each spawn, pause or delete job there; neither the provider
  token nor runtime credentials reach the sandbox.
- **Keeper policy**: Amp decides when its threads pause and are deleted. sbx
  keeps them awake while the provider runs and never deletes a paused thread's
  sandbox for its age, since the disk is the thread's state.
- **Pause and resume hooks**: before a running thread's sandbox pauses (and
  before its project snapshot), sbx stops the thread's `amp orb bootstrap`,
  keeping the headless executor and the app; if it does not stop, the pause
  fails rather than saving it. A resume that was not a spawn (a preview woke
  the sandbox) stops an older snapshot's bootstrap too, which would otherwise
  keep retrying its expired `ORB_TOKEN`. The next spawn brings a fresh token.

## Set it up

1. Onboard the repository in sbx first and pass the
   [acceptance checklist](agent-install.md#acceptance-checklist). Amp uses the
   same recipe, services and variables; there is no separate Amp configuration
   for them.
2. In Amp **Settings → Orbs**, add a self-hosted provider and turn **Repository
   Setup** off: sbx puts the checkout at `/home/user/workspace/repo`, linked to
   the recipe's workdir, with renewable git credentials.
3. In sbx **Extensions → Coding agents → Amp**, paste the provider token, choose a
   default repository if you like, and enable it. The token goes into the secret
   store; saving installs the host CLI if missing and starts `sbx-amp`. On the
   host you can do the same without putting the token in a command line:

   ```sh
   read -r -s AMP_TOKEN
   printf '%s\n' "$AMP_TOKEN" | sbx-admin connect amp provider_token=- enabled=true repo=owner/repository
   unset AMP_TOKEN
   ```

4. Check that the provider shows as connected in Amp. A running `sbx-amp`
   service alone does not prove Amp accepted the token.
5. To build the templates now instead of waiting for the scheduler:
   `sbx-admin amp build [--repo owner/repository]`.

Use one controller per provider token; do not run another `amp orb provider`
beside the service.

## Routing threads to repositories

Amp's [dispatch contract](https://ampcode.com/docs/orbs/self-hosted#dispatch-contract)
passes `ORB_OPERATION`, `ORB_ID`, `ORB_TYPE`, `ORB_SNAPSHOT_ID`, `ORB_TOKEN` and
similar fields, but no repository URL, branch or user. sbx therefore picks the
repository from the orb type:

- `sbx-admin amp routes` lists every trusted repository with a recipe and its
  orb type name (a slug of `owner/repo`).
- A thread with an orb type runs that repository. Without one, it runs the
  connector's default repository, or the only configured repository.
- An ambiguous or unknown route fails before any service is created; an existing
  orb keeps its route.
- Orb sandboxes belong to the installation's first administrator, because the
  dispatch contract does not identify the Amp user.

For several repositories, add their orb types to the provider in Amp and pick
the matching type per project. Match the Amp project to the repository yourself;
dispatch gives sbx nothing to cross-check.

A spawn returns once the checkout and service env are ready; setup and start
continue in sbx. Amp still runs the repository's `.agents/setup` and
`.agents/resume`, so keep those idempotent and do not duplicate database resets
or server starts there. sbx writes `.amp/portals/sbx-previews.json` (excluded
from git) so Amp's Portal tab links to the sandbox's preview URLs.

A spawn that fails after it created its sandbox deletes it again, as a delete
job would, so no VM is left running for a thread that never started; Amp's next
spawn starts over. A spawn counts as failed when Amp's bootstrap is not seen
running in the sandbox within 20 seconds of its launch; each launch carries a
non-secret id, so a bootstrap an earlier spawn left running does not count. A spawn that resumed an
existing orb's sandbox keeps it on failure, and the cleanup never touches a
registration under the orb's name that it did not make itself. If the cleanup
itself fails, the control service's log says so. A delete job
(`ORB_OPERATION=delete`) uses the same path, so it fails for the same reason
while the cause is there (the runtime not answering, say): fix what the log
names, send the delete again
(`ORB_OPERATION=delete ORB_ID=ID sbx-admin amp dispatch`), then check with `sbx-admin amp list` that the
sandbox is gone.

## Project snapshots

Amp can ask for a project snapshot (`ORB_SNAPSHOT_ID`) when a thread pauses, and
start later threads of the same project from it. sbx publishes such a cache only
from a cleaned copy:

1. It takes a private filesystem checkpoint of the thread's sandbox and starts a
   temporary copy with all outbound connections blocked.
2. In the copy, it runs the recipe's executable `.sbx/snapshot` hook, then
   removes platform env, git and agent credentials, SSH keys, shell history,
   swap, and every path extensions list in `scrub_paths`.
3. It publishes a filesystem-only snapshot of the copy and deletes the copy and
   the private checkpoint, even on failure.

The hook removes the repository's own secret files and writable app data and
leaves a clean git worktree. Choose the paths from the repository, for example:

```sh
#!/bin/sh
set -eu
rm -f .env.local
rm -rf .development-data
```

Without an executable hook, snapshot jobs fail. The hook must not touch external
databases. Each new thread still gets its own services and secrets before its
app starts; resuming the same thread keeps its own files and memory.

Snapshots need filesystem-only checkpoints in the runtime, which upstream ships
disabled. On the host, `sbx_control/connectors/amp/checkpoints/build.sh
OUTPUT_DIRECTORY` (in the active release, `/opt/sbx-control/current`) builds an
orchestrator binary and an API binary with the option; build the API image from
`OUTPUT_DIRECTORY/Dockerfile` on the runtime's API image, name both in the
`amp` settings and apply them:

```sh
sbx-admin config set amp '{"checkpoint_api_image": "TAG", "orchestrator_binary": "OUTPUT_DIRECTORY/orchestrator"}'
sbx-admin network apply
```

The connector then adds its compose file to the runtime and its keys to the
runtime's private `.env`. Replacing the orchestrator recreates the runtime,
which existing sandboxes do not survive, paused ones included
([limits](reference.md#limits)), so `network apply` refuses while any exist:
set this up before creating sandboxes, or delete them first, and keep the
previous images for rollback. Clearing both settings and applying again goes
back to the published runtime. Exporting a snapshot also needs room for a
temporary VM the size of the source.

## Inspect and clean up

```sh
sbx-admin amp list                 # Amp's sandboxes, paused ones included
sbx-admin amp snapshots list
sbx-admin amp snapshots delete KEY
journalctl -u sbx-amp -n 50
```

Use the ordinary sandbox views, API and `sbx sandbox` commands to inspect
setup, restart the app or change env. Delete threads in Amp while the provider is
connected, then check that their sandboxes and owned resources are gone;
archiving a thread only pauses it. Disconnecting the provider stops jobs and
keeps existing disks. Remove a single orphan with
`ORB_OPERATION=delete ORB_ID=ID sbx-admin amp dispatch`.

Project caches outlive the thread that made them. Invalidate a cache in Amp's
project settings first, then delete it in sbx; deleting it in sbx alone does not
tell Amp.
