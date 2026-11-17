# Install sbx and onboard a repository

This is the one onboarding guide, for a person or a coding agent. It goes from a
fresh server to a useful first repository and ends in the
[acceptance checklist](#acceptance-checklist).

Start from the owner's repository and server access. Read the [README](../README.md)
and `install.sh` before changing the server. Do not assume a domain, GitHub App,
external account or existing recipe; ask only for what you cannot discover.

Work through the steps in order and keep the current step visible. For each step,
say what you are setting up, do it through the exposed CLI/API, check the result,
then move on. Do not stop for approval between routine steps. Ask a focused
question when the owner must choose a dataset, supply access, or complete an
interactive login, and wait for the answer before dependent work. Resolve a
failed step before presenting it as done.

## Steps

1. **Server.** Check Ubuntu 24.04, `/dev/kvm`, memory, disk and network access
   against the [host requirements](../README.md#host-requirements). Explain any
   prerequisite only the owner can fix. What the server needs depends on the
   [runtimes](reference.md#runtimes) it will run: Embed, the built-in runtime,
   runs sandboxes on the server itself and needs `/dev/kvm` and the memory and
   disk the host requirements list. A server without `/dev/kvm` (or too
   small for Embed) runs them at E2B Cloud: ask the owner for an E2B API key
   and a domain whose `D` and `*.D` point at the server (E2B's sandboxes call
   it back at `login.D`), and let the owner type the key into the server
   themselves (`read -rs E2B_CLOUD_API_KEY; export E2B_CLOUD_API_KEY`), then
   install with `sudo --preserve-env=E2B_CLOUD_API_KEY sh install.sh --domain D
   --email E`. Expect sandboxes within the E2B plan's limits (Hobby: an hour
   before an idle sandbox pauses, 8 vCPUs, 8 GiB, 10 GiB disk, 10 at once:
   size the recipe within them) and no managed servers on the host's private
   network. Without KVM and without a key the installer goes on with no
   runtime enabled: the owner then adds E2B Cloud in the setup wizard's
   "Sandbox runtime" step (the key typed into the browser, which seals it
   into the secret store) or on the server (`sbx-admin runtime add
   e2b-cloud`, the key on stdin); the domain must be connected first. Any
   server can add E2B Cloud later the same ways, or in Settings → Sandbox
   runtimes. An agent does it from the laptop too: on a fresh server with
   `sbx setup` and the setup link, later with `sbx runtimes` and an admin's
   token (step 4), the owner handing over the key as a file or a pipe, never
   in the chat or a command line. Download the latest release's archive
   (or the release the owner names), check its `.sha256` and run
   `sh install.sh` from the unpacked directory as root, as the
   [README](../README.md#install) shows; keep its progress visible. A git
   checkout needs its web app built first (Node 22).
2. **Control plane.** Open the printed setup link. Guide the owner through IP
   access or a domain and the admin login; let them type credentials themselves.
   With a domain, check the DNS records and ports the host requirements list
   (the `_acme-challenge` NS record and port 53 for `--dns delegated`). Each
   person has their own password; the owner adds teammates under Settings →
   People (or `sbx-admin user add EMAIL`), which gives a one-time link to set
   it. To sign in through an identity provider, suggest OIDC with
   `allowed_emails`.
   Confirm the control service, runtimes and secret store are ready
   (`sbx-admin doctor`, `sbx-admin runtime verify`, `sbx-admin store status`).
   The runtimes' checks include one that starts a sandbox and fetches this
   server's public address from inside it: template builds, git pushes and
   catalog actions all need that address to work from where sandboxes run.
3. **Repository access.** Connect GitHub on the web app's **Extensions** page
   and let the owner select a repository. A public repository URL works
   without a GitHub App.
4. **Desktop CLI.** On the machine with a checkout of the repository, install
   `sbx` from the server's **CLI & access** page and run `sbx login`. The
   desktop CLI runs on macOS only; on Linux or in CI, use the
   [HTTP API](reference.md#http-api) with a token the owner makes on the host
   (`sbx-admin token create EMAIL --label LABEL`, shown once).

   **A server without `/dev/kvm`, set up without a browser.** It installs
   with no sandbox runtime, and the setup can't finish without one (so
   `sbx login` can't sign in yet). The agent takes the wizard's steps (step
   2) with the setup link install.sh printed, kept out of argv and the chat,
   and the CLI from the release archive it installed from (`python3
   cli/sbx.py setup ...`: the server serves its CLI installer once set up):

   ```sh
   export SBX_SETUP_LINK="$(cat "$LINK_FILE")"   # or: --link-file "$LINK_FILE" on each command
   sbx setup status
   sbx setup address --domain sbx.example.com --email ops@example.com   # waits for HTTPS
   sbx setup runtime add e2b-cloud --plan hobby --secret E2B_CLOUD_API_KEY < "$KEY_FILE"
   sbx setup admin ops@example.com < "$PASSWORD_FILE"
   sbx setup finish --admin ops@example.com < "$PASSWORD_FILE"
   sbx login
   ```

   Add `--insecure` while the server still answers with its self-signed
   certificate (before the address step's certificate). E2B Cloud needs the
   domain: its sandboxes call the server back.

   **Runtimes later, from the laptop.** An agent signed in as an admin adds,
   changes, tests and removes runtimes itself, for example E2B Cloud on a
   server that runs Embed, or another plan:

   ```sh
   sbx runtimes available                        # e2b-cloud can be added; its plans and settings
   sbx runtimes add e2b-cloud --plan hobby --secret E2B_CLOUD_API_KEY < "$KEY_FILE"
   sbx runtimes test e2b-cloud                   # each check ok / NO; exit 1 when one fails
   ```

   The owner gives the key as a file only they and the agent read, or pipes
   it in (`pbpaste | sbx runtimes add ...`); it never goes on a command line
   or into the chat. Choose the plan the owner's E2B team is on (`--plan
   pro`; on Pro, `--set E2B_CLOUD_MAX_CPU=N` when E2B raised it). `add`
   waits until the server has applied it and restarted, and prints the
   outcome. On an installation whose secret store is still the local one it
   is refused (409); then the owner runs `sbx-admin runtime add e2b-cloud`
   on the server with the key on stdin. The base template then builds on it
   (about ten minutes); `sbx runtimes test` says when it is built.
5. **Set up the repository.** In the checkout, `sbx repo connect --agent`
   starts Claude Code (or `--agent codex`) with the server's setup brief.
   Without that agent installed, it prints the brief for another agent to
   follow (the API serves it at
   `GET /api/repos/OWNER/NAME/setup-brief`). The
   [setup brief](../control/sbx_control/template_files/setup-brief.md) is the
   one procedure from discovery to a verified first launch: it chooses services
   with [service selection](service-selection.md), declares every variable,
   writes a small [recipe](reference.md#recipe), sizes it with the
   [resource guide](../control/sbx_control/template_files/resource-guide.md)
   and launches it. For preloaded development data, see
   [development baselines](reference.md#development-baselines).
6. **Acceptance.** Run the [acceptance checklist](#acceptance-checklist).
7. **Coding agents.** Ask the owner which coding agents the team uses. For
   Claude Code cloud sessions, follow [Claude Code](claude-code.md); for Amp
   threads, follow [Amp](amp-orbs.md). Each needs a key only the owner can copy
   (from claude.ai, or from Amp's settings) and paste on the **Extensions**
   page; never ask for it in chat. Check that the card shows the connector as
   connected and start one session or thread to confirm its sandbox comes up.
   Other agents need nothing here: they reach sandboxes over SSH (`sbx
   ssh-config`) or with `sbx open NAME AGENT`.
8. **Handoff.** Give the owner working preview links and the commands to start
   another sandbox (`sbx up`), read logs (`sbx logs`), change variables
   (`sbx env`) and delete a sandbox (`sbx down`). Offer a setup PR
   (`sbx recipe pr`), PR previews or a prebuilt template once the basic launch
   works.

Do not put private values in chat, recipes, commits, templates or logs. Tell the
owner what succeeded, what is limited, and which actions remain theirs.

## Acceptance checklist

Every sandbox is a development environment of its own: it must be able to
migrate, seed and reset its data without changing another sandbox's data or
schema. Shared service infrastructure keeps this cheap; the databases, branches
or namespaces on it are per sandbox. This is the one place that rule is stated;
other docs link here.

Before you start, make sure the recipe gives each sandbox:

- its own database, branch or namespace, provisioned before `.sbx/setup` runs,
  with a login that may run the repository's normal migrations (schema changes
  included);
- setup that runs the normal migrations and development seeds and is safe to
  repeat;
- SQLite files, uploads and other app state in its own filesystem or in a
  resource of its own;
- a template, if any, with tools and dependencies only: no credentials and no
  other sandbox's data.

Then check, with two disposable, named sandboxes A and B:

- [ ] Both launch cleanly from the recipe and pass a useful app action.
- [ ] Migrate and seed both. Change or reset A's data; B still has its original
      data and schema.
- [ ] Restart A (`sbx sandbox restart A`), then pause and resume it
      (`sbx sandbox pause|resume A`, or `POST /api/sandboxes/A/pause` and `/resume`):
      its working data is still there.
- [ ] Delete A (`sbx down A`): B still works, and A's owned resources (its
      database, branch, role or namespace) are gone while the shared servers stay.
- [ ] No secret value appears in the recipe, the template, commits or logs.

Record what you verified and what you did not (for example optional providers
that were never configured).
